"""Generate two paraphrases and a typo variant of each DEV routing/quantitative question via Codex (GPT),
for a cross-model-family robustness eval. Writes evals/cases/paraphrase.jsonl."""

import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "evals" / "cases"
CODEX_BIN = "codex"
TIMEOUT_S = 600
MAX_LEN = 300
DIGIT_RUN = re.compile(r"\d{4,}")
SUFFIXES = (("p1", "paraphrase"), ("p2", "paraphrase"), ("t1", "typo"))

RULES = """Write a robustness eval set for a claims-data QA system used by property insurance claims staff.

For each question, produce two genuine paraphrases p1/p2 (reworded, not reordered; same meaning and intent; \
natural employee wording) and one typo variant t1 (one or two keyboard typos in words, never inside a \
number). Keep every claim number and four-digit year exact, and keep the region, state, peril, measure, and \
period meaning (period wording may change, e.g. "second quarter of 2025" for "Q2 2025", as long as the \
digits and meaning survive). Preserve the question's kind exactly: an injection attempt, off-topic, small \
talk, a code request, deliberate vagueness, or a future/out-of-2024-2026 year question stays that way in \
every variant.

Reply with strict JSON only, no prose, no markdown, no fences, exactly: \
{"items": [{"id": "...", "p1": "...", "p2": "...", "t1": "..."}]}. One object per input id, same id, never \
add, drop, merge, or reorder ids."""


@dataclass(frozen=True)
class Original:
    id: str
    q: str
    user: str
    route: str
    expect: str
    extra: dict[str, Any]  # remaining output fields, already in their required output order


def load_originals() -> list[Original]:
    def rows(name: str) -> list[dict[str, Any]]:
        with (CASES_DIR / name).open() as fh:
            return [cast(dict[str, Any], json.loads(line)) for line in fh if line.strip()]

    def make(row: dict[str, Any], extra: dict[str, Any]) -> Original:
        return Original(row["id"], row["q"], row["user"], row["route"], row["expect"], extra)

    originals: list[Original] = []
    for row in rows("routing.jsonl"):
        if row["split"] == "dev" and row.get("refusal_reason") not in ("empty", "too_long"):
            originals.append(make(row, {"refusal_reason": row["refusal_reason"]}))
    for row in rows("quantitative.jsonl"):
        if row["split"] == "dev":
            originals.append(make(row, {"match": row["match"], "gold_sql": row["gold_sql"]}))
    return originals


def build_prompt(items: list[Original], failures: dict[str, list[str]] | None = None) -> str:
    if failures is None:
        payload: list[dict[str, Any]] = [{"id": o.id, "q": o.q} for o in items]
        return f"{RULES}\n\nQuestions:\n{json.dumps(payload)}"
    payload = [{"id": o.id, "q": o.q, "problems_last_time": failures[o.id]} for o in items]
    return (
        f"{RULES}\n\nYour previous answer for these ids broke the rules above, in the way described for "
        f"each one. Send corrected p1/p2/t1 for exactly these ids.\n\n{json.dumps(payload)}"
    )


def extract_json_object(text: str) -> dict[str, Any]:
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    text = fence.group(1) if fence else text
    start = text.find("{")
    if start == -1:
        return {}
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return {}
                return cast(dict[str, Any], obj) if isinstance(obj, dict) else {}
    return {}


def call_codex(prompt: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "last_message.txt"
        cmd = [CODEX_BIN, "exec", "--sandbox", "read-only", "--skip-git-repo-check", "--output-last-message"]
        try:
            # Run from an empty directory, so the model has no repository to read, the held-out set included.
            result = subprocess.run(
                [*cmd, str(out_path)], input=prompt, capture_output=True, text=True, timeout=TIMEOUT_S, cwd=tmp
            )
        except subprocess.TimeoutExpired:
            print(f"codex exec timed out after {TIMEOUT_S}s", file=sys.stderr)
            return ""
        text = out_path.read_text().strip() if out_path.exists() else ""
        if text:
            return text
        if result.returncode != 0:
            print(f"codex exec exited {result.returncode}: {result.stderr[:500]}", file=sys.stderr)
        return result.stdout


def normalize(text: str) -> str:
    return " ".join(text.split()).casefold()


def validate(original: Original, item: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    digit_runs = DIGIT_RUN.findall(original.q)
    norm_q = normalize(original.q)
    variants: dict[str, str] = {}
    for key, _kind in SUFFIXES:
        value = item.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key}: missing or empty")
            continue
        if len(value) > MAX_LEN:
            problems.append(f"{key}: {len(value)} chars, over the {MAX_LEN} limit")
        if normalize(value) == norm_q:
            problems.append(f"{key}: identical to the original apart from case and whitespace")
        missing_digits = [run for run in digit_runs if run not in value]
        if missing_digits:
            problems.append(f"{key}: dropped digits {', '.join(missing_digits)} from the original")
        variants[key] = value
    if "p1" in variants and "p2" in variants and normalize(variants["p1"]) == normalize(variants["p2"]):
        problems.append("p1 and p2 are identical apart from case and whitespace")
    return problems


def call_and_validate(items: list[Original], prompt: str) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    parsed = extract_json_object(call_codex(prompt))
    raw_items = parsed.get("items")
    seen: dict[str, list[dict[str, Any]]] = {}
    if isinstance(raw_items, list):
        for entry in raw_items:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                seen.setdefault(cast(str, entry["id"]), []).append(cast(dict[str, Any], entry))
    passed: dict[str, dict[str, Any]] = {}
    failed: dict[str, list[str]] = {}
    for o in items:
        matches = seen.get(o.id, [])
        if len(matches) != 1:
            failed[o.id] = [f"id appeared {len(matches)} times in codex response, want 1"]
        elif problems := validate(o, matches[0]):
            failed[o.id] = problems
        else:
            passed[o.id] = matches[0]
    return passed, failed


def main() -> int:
    originals = load_originals()
    passed, failed = call_and_validate(originals, build_prompt(originals))
    if failed:
        retry_items = [o for o in originals if o.id in failed]
        retry_passed, failed = call_and_validate(retry_items, build_prompt(retry_items, failed))
        passed.update(retry_passed)
    for eid in sorted(failed):
        print(f"dropped {eid}: {'; '.join(failed[eid])}", file=sys.stderr)
    out_path = CASES_DIR / "paraphrase.jsonl"
    lines_written = covered = 0
    with out_path.open("w") as fh:
        for o in originals:
            item = passed.get(o.id)
            if item is None:
                continue
            covered += 1
            for key, kind in SUFFIXES:
                record = {
                    "id": f"{o.id}-{key}",
                    "split": "dev",
                    "of": o.id,
                    "variant": kind,
                    "user": o.user,
                    "q": item[key],
                    "route": o.route,
                    "expect": o.expect,
                    **o.extra,
                }
                fh.write(json.dumps(record) + "\n")
                lines_written += 1
    dropped = ", ".join(sorted(failed)) or "none"
    print(f"wrote {lines_written} variants covering {covered}/{len(originals)} originals; dropped: {dropped}")
    # A case that failed validation twice has zero of its required 2 paraphrases + 1 typo written, not a
    # partial set; failing loud here is what makes that silent shrink of the set visible to the caller.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
