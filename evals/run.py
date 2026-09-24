"""python -m evals.run --write | --check [--split dev|heldout|all]"""

import argparse
import asyncio
import os
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any

from psycopg.types.json import Jsonb

from app import db
from app.config import ROOT, settings
from app.identity import principals
from app.sandbox.client import Sandbox
from evals import report
from evals.answers import score_answers, score_why
from evals.guards import score_hostile, score_verifier
from evals.leaks import load_secrets, note_sweep, score_permissions
from evals.ocr import Stored, score_extraction, score_ocr_answers, stored_fields
from evals.outcome import Outcome, run_case
from evals.retrieval import score_retrieval
from evals.robustness import score_robustness
from evals.routing import routing_verdicts, score_abstention, score_latency, score_refusal, score_routing
from evals.splits import SPLITS, Case, LockMismatch, Split, load, verify_lock
from evals.sql import execution_misses, score_sql


def preflight() -> list[str]:
    """The eval measures the whole system, so a missing model or sandbox would score a degraded one."""
    problems = []
    s = settings()
    for name, path in (("embedding", s.embed_model_path), ("reranker", s.rerank_model_path)):
        if path is None or not path.exists():
            problems.append(f"the {name} model is not here ({path}); run make eval, which runs in the app image")
    url = f"{Sandbox().url}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3):
            pass
    except (urllib.error.URLError, OSError) as exc:
        problems.append(f"sandboxd did not answer at {url} ({exc}); run make up first")
    return problems


def git_commit() -> str | None:
    if os.environ.get("GIT_COMMIT"):
        return os.environ["GIT_COMMIT"]
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head
        ref = head.removeprefix("ref: ")
        if (git / ref).exists():
            return (git / ref).read_text().strip()
        packed = (line.split(" ", 1) for line in (git / "packed-refs").read_text().splitlines() if " " in line)
        return next((sha for sha, name in packed if name == ref), None)
    except OSError:
        return None


async def run_all(cases: list[Case]) -> list[Outcome]:
    return [await run_case(case) for case in cases]


async def evaluate(split: Split, stored: Stored) -> dict[str, Any]:
    routing = await run_all(load(split, "routing"))
    quant = await run_all(load(split, "quantitative"))
    qualitative = load(split, "qualitative")
    qual = await run_all(qualitative)
    why = await run_all(load(split, "why"))
    ocr = await run_all(load(split, "ocr"))
    sql, sql_verdicts = score_sql(quant)
    answers, answer_verdicts = score_answers(qual)
    why_section, why_verdicts = score_why(why)
    ocr_section, ocr_verdicts = score_ocr_answers(ocr)
    verdicts = routing_verdicts(routing) + sql_verdicts + answer_verdicts + why_verdicts + ocr_verdicts
    section: dict[str, Any] = {
        "cases": {"routing": len(routing), "quantitative": len(quant), "qualitative": len(qual), "why": len(why)},
        "routing": score_routing(routing),
        "refusal": score_refusal(routing),
        "abstention": score_abstention(routing, verdicts),
        "sql": sql,
        "retrieval": await score_retrieval(qualitative),
        "answers": answers,
        "why": why_section,
        "ocr": ocr_section,
    }
    section["cases"]["ocr"] = len(ocr)
    ran = routing + quant + qual + why + ocr
    if split == "dev":
        users = sorted(principals())
        secrets = load_secrets(stored)
        probes = [await run_case(case, user) for case in load(split, "permissions") for user in users]
        section["permissions"] = score_permissions(probes, secrets)
        section["permissions"]["search"], own_hits = await note_sweep(load(split, "permissions"), users, secrets)
        section["permissions"]["controls"]["own_canary_hits"] = own_hits
        variants = await run_all(load(split, "paraphrase"))
        executed = {o.case["id"]: v.right for o, v in zip(quant, sql_verdicts, strict=True)}
        routed = {o.case["id"]: o.label == o.case["route"] for o in routing + quant}
        quant_variants = [o for o in variants if "gold_sql" in o.case]
        variant_executed = {case_id: not found for case_id, found in execution_misses(quant_variants).items()}
        section["robustness"] = score_robustness(variants, routed, executed, variant_executed)
        section["cases"] |= {"permissions": len(probes), "paraphrase": len(variants)}
        ran += probes + variants
    section["latency"] = score_latency(ran)
    return section


async def collect(splits: list[Split]) -> dict[str, Any]:
    try:
        stored = await stored_fields()
        fresh: dict[str, Any] = {split: await evaluate(split, stored) for split in splits}
        fresh["shared"] = {
            "ocr_extraction": score_extraction(stored),
            "verifier": score_verifier(),
            "hostile_sql": score_hostile(),
        }
        commit = git_commit()
        for split in splits:
            await db.write(
                "INSERT INTO ops.eval_runs (run_id, split, mode, git_commit, metrics) VALUES (%s, %s, %s, %s, %s)",
                (uuid.uuid4(), split, "none", commit, Jsonb(report.headline(fresh[split], fresh["shared"]))),
            )
        return fresh
    finally:
        await db.close_all()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="run every case and write evals/report.json")
    action.add_argument("--check", action="store_true", help="run every case and compare with evals/report.json")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="all")
    args = parser.parse_args(argv)
    try:
        verify_lock()
    except LockMismatch as exc:
        print(f"refusing to run, evals/heldout.lock does not match the held-out files: {exc}", file=sys.stderr)
        return 2
    if problems := preflight():
        print("\n".join(problems), file=sys.stderr)
        return 2
    splits: list[Split] = list(SPLITS) if args.split == "all" else [args.split]
    fresh = asyncio.run(collect(splits))
    print(report.summary(fresh))
    if args.write:
        report.REPORT.write_text(report.dump(report.read() | fresh))
        print(f"wrote {report.REPORT.relative_to(ROOT)}")
        return 0
    differences = report.compare(report.read(), fresh, [*splits, "shared"])
    if differences:
        print(f"\n{len(differences)} differences from evals/report.json:\n{report.table(differences)}")
        return 1
    print("matches evals/report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
