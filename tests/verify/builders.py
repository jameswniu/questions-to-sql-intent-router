from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from app.answer.format import NumberRef, change_refs, ref
from app.answer.types import Claim, Draft, Evidence
from app.sources.documents import Hit

# Two periods of paid losses and of a denial rate, shaped as the quantitative workflow returns them.
PAID_ROWS: tuple[dict[str, Any], ...] = (
    {"period": "current", "value": Decimal("35768928.46")},
    {"period": "prior", "value": Decimal("24345938.49")},
)
RATE_ROWS: tuple[dict[str, Any], ...] = (
    {"period": "current", "numerator": 97, "denominator": 516, "value": Decimal("0.18798449612403100775")},
    {"period": "prior", "numerator": 11, "denominator": 449, "value": Decimal("0.02449888641425389755")},
)
PAID_TEXT = "Paid losses were $35,768,928 in 2025 and $24,345,938 in 2024, up $11,422,990 (46.9%)."
RATE_TEXT = "Denial rate for wind claims was 18.8% in 2025 and 2.4% in 2024, up 16.3 points (667.3%)."


def paid_refs() -> tuple[NumberRef, ...]:
    now, then = (ref(row["value"], "currency", "value", i, "value") for i, row in enumerate(PAID_ROWS))
    return now, then, *change_refs(now.value, then.value, "currency", (0, 1))


def rate_refs(rows_offset: int = 0) -> tuple[NumberRef, ...]:
    source = "numerator / denominator"
    now, then = (ref(row["value"], "percent", "value", i + rows_offset, source) for i, row in enumerate(RATE_ROWS))
    rows = (rows_offset, rows_offset + 1)
    return now, then, *change_refs(now.value, then.value, "percent", rows, source)


def paid_claim(text: str = PAID_TEXT, citations: tuple[str, ...] = ()) -> Claim:
    return Claim(text, paid_refs(), citations)


def draft(*claims: Claim, caveats: tuple[str, ...] = ()) -> Draft:
    return Draft(claims, caveats)


def hit(chunk_id: str, region: str | None = None, body: str = "Wind claims are inspected within 30 days.") -> Hit:
    doc_id, _, anchor = chunk_id.partition("#")
    kind = "note" if region else "guideline"
    return Hit(chunk_id, doc_id, anchor, doc_id, "Handling", body, kind, 1.0, 1, None, False, None, region)


def evidence(
    rows: Iterable[dict[str, Any]] = PAID_ROWS,
    hits: Iterable[Hit] = (),
    sandbox: Iterable[dict[str, Any]] = (),
    scan_fields: Iterable[dict[str, Any]] = (),
) -> Evidence:
    return Evidence(tuple(rows), tuple(hits), tuple(sandbox), tuple(scan_fields))
