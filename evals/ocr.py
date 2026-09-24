import json
import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app import db
from app.config import DATA_DIR
from app.identity import principals
from evals.metrics import character_error_rate
from evals.outcome import Outcome, Verdict, rate

TRUTH = DATA_DIR / "scans" / "truth.jsonl"
MONEY = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?")
# The two ways a scan answer tells the asker not to trust the scanned total.
FLAGGING = ("couldn't be read reliably", "doesn't match")
Stored = dict[tuple[str, str], tuple[str | None, bool]]


def truth() -> dict[str, dict[str, Any]]:
    return {scan["doc_id"]: scan for scan in map(json.loads, TRUTH.read_text().splitlines())}


async def stored_fields() -> Stored:
    """Every scan field as ingested, read as the supervisor, whose regions cover every scan."""
    supervisor = next(p for p in principals().values() if p.kind == "supervisor")
    found = await db.run(supervisor, "SELECT doc_id, field, value, flagged FROM rag.scan_fields", row_cap=10_000)
    if found.truncated:
        raise RuntimeError("rag.scan_fields has more rows than the eval reads")
    return {(doc_id, field): (value, flagged) for doc_id, field, value, flagged in found.rows}


def amount(text: str | None) -> Decimal | None:
    if text is None:
        return None
    try:
        return Decimal(text.replace("$", "").replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return None


def field_type(name: str) -> str:
    return "line_item" if name.startswith("line_") else name


def expected_fields(scan: dict[str, Any]) -> dict[str, str]:
    fields = {name: str(scan[name]) for name in ("claim_number", "vendor", "date", "total")}
    fields |= {f"line_{n}": f"{item['description']} | {item['amount']}" for n, item in enumerate(scan["items"], 1)}
    return fields


def same(name: str, stored: str | None, expected: str) -> bool:
    """Amounts compare as numbers, so 595779 and 595779.00 agree; everything else compares as text."""
    if stored is None:
        return False
    if field_type(name) == "total":
        return amount(stored) is not None and amount(stored) == amount(expected)
    if field_type(name) == "line_item":
        text, _, value = stored.rpartition(" | ")
        want_text, _, want_value = expected.rpartition(" | ")
        return text == want_text and amount(value) is not None and amount(value) == amount(want_value)
    return stored == expected


def _canonical(text: str | None) -> str:
    value = amount(text)
    return f"{value:.2f}" if value is not None else (text or "").replace("$", "").replace(",", "").replace(" ", "")


def score_extraction(stored: Stored) -> dict[str, Any]:
    """A field should be flagged when what was stored differs from the page, and a total also when the scan is a
    planted mismatch: the page is read right, but it disagrees with what was paid."""
    scans = truth()
    exact: dict[str, list[bool]] = {}
    tp = fp = fn = 0
    totals = []
    for doc_id, scan in sorted(scans.items()):
        expected = expected_fields(scan)
        for name in sorted(set(expected) | {field for doc, field in stored if doc == doc_id}):
            value, flagged = stored.get((doc_id, name), (None, False))
            right = name in expected and same(name, value, expected[name])
            if name in expected:
                exact.setdefault(field_type(name), []).append(right)
            should = not right or (name == "total" and scan["mismatch"] is not None)
            tp, fp, fn = tp + (should and flagged), fp + (flagged and not should), fn + (should and not flagged)
        totals.append((_canonical(str(scan["total"])), _canonical(stored.get((doc_id, "total"), (None, False))[0])))
    return {
        "scans": len(scans),
        "missing_scans": sorted(set(scans) - {doc for doc, _ in stored}),
        "total_cer": round(character_error_rate(totals), 4),
        "total_chars": sum(len(reference) for reference, _ in totals),
        "exact_match": {kind: rate(sum(marks), len(marks)) for kind, marks in sorted(exact.items())},
        "flag_precision": rate(tp, tp + fp),
        "flag_recall": rate(tp, tp + fn),
    }


def judge_text(expect: str, text: str, scan: dict[str, Any]) -> str | None:
    """Why an answer to a scan question fails its expectation, or None when it passes. The only amounts it may
    state are the total printed on the page and the amount paid; any other is a wrong number, whatever else it says.
    A planted mismatch must be flagged. A degraded scan may state the true total or flag it. A clean scan must state
    its true total without a flag."""
    stated = {Decimal(found[1:].replace(",", "")) for found in MONEY.findall(text)}
    printed, paid = Decimal(str(scan["total"])), Decimal(str(scan["ledger_total"]))
    wrong = sorted(str(value) for value in stated - {printed, paid})
    flags = any(phrase in text for phrase in FLAGGING)
    if wrong:
        return f"stated {', '.join(wrong)}"
    if expect == "flag":
        return None if flags else "did not flag the mismatch"
    if expect == "no_wrong_number":
        return None if flags or printed in stated else "neither stated the true total nor flagged it"
    if flags:
        return "flagged a clean scan"
    return None if printed in stated else "did not state the true total"


def score_ocr_answers(outcomes: Sequence[Outcome]) -> tuple[dict[str, Any], list[Verdict]]:
    scans = truth()
    failures: dict[str, str] = {}
    by_expect: dict[str, list[bool]] = {}
    verdicts = []
    for o in outcomes:
        if o.label != o.case["route"]:
            problem: str | None = f"routed to {o.label}"
        elif not o.answered or o.answer is None:
            problem = f"not answered ({o.outcome})"
        else:
            problem = judge_text(o.case["expect"], o.answer.text, scans[o.case["scan"]])
        if problem is not None:
            failures[o.case["id"]] = problem
        by_expect.setdefault(o.case["expect"], []).append(problem is None)
        verdicts.append(Verdict(o.answered, problem is None))
    section = {
        "pass": rate(len(outcomes) - len(failures), len(outcomes)),
        "by_expect": {expect: rate(sum(marks), len(marks)) for expect, marks in sorted(by_expect.items())},
        "failures": dict(sorted(failures.items())),
    }
    return section, verdicts
