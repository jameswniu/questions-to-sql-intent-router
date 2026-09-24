"""python -m evals.run --write | --check [--split dev|heldout|all]
python -m evals.run --live --write [--runs N] [--split dev|heldout|all]"""

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
from app.llm.client import LLM, LiveConfigError, from_env
from app.sandbox.client import Sandbox
from evals import live, report
from evals.answers import score_answers, score_why
from evals.guards import score_hostile, score_verifier
from evals.leaks import dead_controls, load_secrets, note_sweep, score_permissions
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


async def run_all(cases: list[Case], llm: LLM | None = None) -> list[Outcome]:
    return [await run_case(case, llm=llm) for case in cases]


async def evaluate(split: Split, stored: Stored, llm: LLM | None = None) -> dict[str, Any]:
    """One split scored. With a model, every case, the dev permission probes included, is asked with live mode on,
    since a leak with a model in the loop matters as much as one without."""
    routing = await run_all(load(split, "routing"), llm)
    quant = await run_all(load(split, "quantitative"), llm)
    qualitative = load(split, "qualitative")
    qual = await run_all(qualitative, llm)
    why = await run_all(load(split, "why"), llm)
    ocr = await run_all(load(split, "ocr"), llm)
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
        "answers": answers,
        "why": why_section,
        "ocr": ocr_section,
    }
    if llm is None:
        # Retrieval searches with no model in the loop, so a live run would only repeat the no-key numbers.
        section["retrieval"] = await score_retrieval(qualitative)
    section["cases"]["ocr"] = len(ocr)
    ran = routing + quant + qual + why + ocr
    if split == "dev":
        users = sorted(principals())
        secrets = load_secrets(stored)
        probes = [await run_case(case, user, llm=llm) for case in load(split, "permissions") for user in users]
        section["permissions"] = score_permissions(probes, secrets)
        section["cases"]["permissions"] = len(probes)
        ran += probes
        # A live run skips the note sweep, which searches the notes with no model in the loop, and the paraphrase
        # set, which would add most of a run's questions again, and their cost, to every run.
        if llm is None:
            section["permissions"]["search"], own_hits = await note_sweep(load(split, "permissions"), users, secrets)
            section["permissions"]["controls"]["own_notes_in_searches"] = own_hits
            variants = await run_all(load(split, "paraphrase"))
            executed = {o.case["id"]: v.right for o, v in zip(quant, sql_verdicts, strict=True)}
            routed = {o.case["id"]: o.label == o.case["route"] for o in routing + quant}
            quant_variants = [o for o in variants if "gold_sql" in o.case]
            variant_executed = {case_id: not found for case_id, found in execution_misses(quant_variants).items()}
            section["robustness"] = score_robustness(variants, routed, executed, variant_executed)
            section["cases"]["paraphrase"] = len(variants)
            ran += variants
    section["latency"] = score_latency(ran)
    if llm is not None:
        section["fallbacks"] = live.score_fallbacks(ran)
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


async def collect_live(splits: list[Split], llm: LLM, runs: int) -> list[dict[str, Any]]:
    """Each split scored once a run with the model in the loop, all in one event loop, the one the model's client
    keeps its connections in. Nothing goes into ops.eval_runs: the dashboard shows the latest row per split, and that
    stays the no-key score."""
    try:
        stored = await stored_fields()
        return [{split: await evaluate(split, stored, llm) for split in splits} for _ in range(runs)]
    finally:
        await db.close_all()


def live_client() -> LLM | None:
    """The model a live run scores, or None once it has said why there is none."""
    try:
        llm = from_env()
    except LiveConfigError as exc:
        print(f"refusing to run live, the model settings don't hold: {exc}", file=sys.stderr)
        return None
    if llm is None:
        print(
            "refusing to run live, LLM_BACKEND is off. Set it to anthropic or vertex, with the settings that backend"
            " needs, in .env or the shell",
            file=sys.stderr,
        )
    return llm


def write_live(splits: list[Split], llm: live.Metered, runs: int) -> int:
    """Scores the splits runs times with the model in the loop and writes the report's live section alone, keeping
    every other section as committed."""
    scored = asyncio.run(collect_live(splits, llm, runs))
    for number, fresh in enumerate(scored, 1):
        print(f"live run {number} of {runs}\n{report.summary(fresh)}")
    found = live.section(llm, scored, git_commit())
    print(live.describe(found))
    if dead := sorted({problem for fresh in scored for split in splits for problem in dead_controls(fresh[split])}):
        print("refusing to report zero leaks that no control backs: " + ", ".join(dead), file=sys.stderr)
        return 1
    report.write(report.read() | {"live": found})
    print(f"wrote the live section of {report.REPORT.relative_to(ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="run every case and write evals/report.json")
    action.add_argument("--check", action="store_true", help="run every case and compare with evals/report.json")
    parser.add_argument("--split", choices=("dev", "heldout", "all"), default="all")
    parser.add_argument(
        "--live", action="store_true", help="score with live mode on and write only the report's live section"
    )
    parser.add_argument("--runs", type=int, help=f"how many times --live scores each split ({live.RUNS} if not given)")
    args = parser.parse_args(argv)
    if args.live and args.check:
        parser.error("--live can't be used with --check: CI never calls a model, so a live score is only written")
    if args.runs is not None and not args.live:
        parser.error("--runs counts live runs, so it needs --live")
    if args.runs is not None and not 1 <= args.runs <= live.MAX_RUNS:
        parser.error(f"--runs must be from 1 to {live.MAX_RUNS}, since every run pays for its model calls")
    llm = live_client() if args.live else None
    if args.live and llm is None:
        return 2
    try:
        verify_lock()
    except LockMismatch as exc:
        print(f"refusing to run, evals/heldout.lock does not match the held-out files: {exc}", file=sys.stderr)
        return 2
    if problems := preflight():
        print("\n".join(problems), file=sys.stderr)
        return 2
    splits: list[Split] = list(SPLITS) if args.split == "all" else [args.split]
    if llm is not None:
        return write_live(splits, live.Metered(llm), args.runs or live.RUNS)
    fresh = asyncio.run(collect(splits))
    print(report.summary(fresh))
    if dead := [problem for split in splits for problem in dead_controls(fresh[split])]:
        print("refusing to report zero leaks that no control backs: " + ", ".join(dead), file=sys.stderr)
        return 1
    if args.write:
        report.write(report.read() | fresh)
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
