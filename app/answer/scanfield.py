import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from app import db
from app.answer.format import NumberRef, long_date
from app.answer.lookup import ANALYST_TEXT
from app.answer.quant import verify_sql
from app.answer.types import Claim, Draft
from app.identity import Principal
from app.semantic.describe import join
from app.sources.scans import ScanField, scan_fields

# Longest first, so "proof of loss" is not read as some other kind that shares a word with it.
SCAN_KINDS = (("proof of loss", "proof-of-loss"), ("invoice", "invoice"), ("estimate", "estimate"))
ASKS_SCAN = re.compile(r"\b(?:invoice|estimate|proof\s+of\s+loss|scan(?:ned)?|receipt|bill)\b", re.IGNORECASE)
FIELD_WORDS = (("vendor", "vendor"), ("contractor", "vendor"), ("dated", "date"), ("date", "date"))
FIELD_LABELS = {"total": "total", "vendor": "vendor", "date": "date"}
PAID_SQL = "SELECT coalesce(sum(amount), 0) AS paid FROM sem.v_payments_net WHERE claim_id = %s AND kind = 'indemnity'"
FLAG_WORDS = {
    "low confidence": "the text was too faint or blurred to read with confidence",
    "format": "what was read isn't in the form a {field} takes",
    "missing": "no {field} was found on the page",
    "does not match payments": "it doesn't match what was paid on the claim",
    "line items do not add up": "the line items don't add up to it",
    "does not match the claim": "the claim number on the page is for a different claim",
}

Kind = Literal["found", "flagged", "not_found", "not_allowed", "clarify"]


@dataclass(frozen=True)
class ScanResult:
    kind: Kind
    text: str
    claim_id: int
    draft: Draft
    fields: tuple[ScanField, ...] = ()
    doc_id: str | None = None
    paid: Decimal | None = None
    sql: str | None = None
    options: tuple[str, ...] = ()


def asks_about_scan(question: str) -> bool:
    return ASKS_SCAN.search(question) is not None


def _scan_kind(question: str) -> tuple[str, str] | None:
    for label, suffix in SCAN_KINDS:
        if re.search(rf"\b{label.replace(' ', r'\s+')}\b", question, re.IGNORECASE):
            return label, suffix
    return None


def _field_asked(question: str) -> str:
    return next((name for word, name in FIELD_WORDS if re.search(rf"\b{word}\b", question, re.IGNORECASE)), "total")


def _label(doc_id: str) -> str:
    return next((label for label, suffix in SCAN_KINDS if doc_id.endswith(f"-{suffix}")), "scanned document")


def plain_reasons(field: ScanField) -> str:
    """The OCR flags in words a claims handler would use, for example 'the line items don't add up to it'."""
    reasons = [r.strip() for r in (field.flag_reason or "missing").split(",") if r.strip()]
    name = FIELD_LABELS.get(field.field, field.field.replace("_", " "))
    return join(FLAG_WORDS.get(reason, reason).format(field=name) for reason in reasons)


def _money(value: Decimal) -> str:
    return f"${value:,.2f}"


def _amount(field: ScanField | None) -> Decimal | None:
    if field is None or field.flagged or field.value is None:
        return None
    try:
        return Decimal(field.value)
    except InvalidOperation:
        return None


async def _paid(principal: Principal, claim_id: int) -> tuple[Decimal, str]:
    sql = verify_sql(PAID_SQL, relations=frozenset({"sem.v_payments_net"}))
    found = await db.run(principal, sql, (claim_id,))
    return Decimal(found.rows[0][0]), sql


def _nothing(kind: Kind, text: str, claim_id: int, options: tuple[str, ...] = ()) -> ScanResult:
    return ScanResult(kind, text, claim_id, Draft((), ()), options=options)


async def answer_scan(principal: Principal, question: str, claim_id: int) -> ScanResult:
    asked = _scan_kind(question)
    label = asked[0] if asked else "scanned document"
    article = "an" if label[0] in "aeiou" else "a"
    # One message whether the claim doesn't exist, sits outside the user's regions or has no such scan,
    # so the answer never says which.
    missing = f"I can't find {article} {label} for claim {claim_id}."
    if principal.kind == "analyst":
        return _nothing("not_allowed", ANALYST_TEXT, claim_id)
    fields = await scan_fields(principal, claim_id)
    docs = sorted({f.doc_id for f in fields if asked is None or f.doc_id.endswith(f"-{asked[1]}")})
    if not docs:
        return _nothing("not_found", missing, claim_id)
    if len(docs) > 1:
        options = tuple(_label(doc) for doc in docs)
        return _nothing("clarify", f"Claim {claim_id} has {join(options)}. Which one?", claim_id, options=options)
    doc_id = docs[0]
    label = _label(doc_id)
    own = tuple(f for f in fields if f.doc_id == doc_id)
    name = _field_asked(question)
    field = next((f for f in own if f.field == name), None)
    if name != "total":
        return _other(claim_id, doc_id, label, name, field, own)
    paid, sql = await _paid(principal, claim_id)
    paid_ref = NumberRef(paid, _money(paid), "paid", 0, "paid")
    value = _amount(field)
    if value is None:
        reasons = plain_reasons(field) if field else f"no total was found on the {label}"
        text = (
            f"The scanned total on the {label} for claim {claim_id} couldn't be read reliably: {reasons}."
            f" The payment record shows {paid_ref.display} paid on this claim."
        )
        claim = Claim(text, (paid_ref,), ())
        return ScanResult("flagged", text, claim_id, Draft((claim,), ()), own, doc_id, paid, sql)
    total_ref = NumberRef(value, _money(value), "value", None, f"rag.scan_fields {doc_id} total")
    text = f"The total on the {label} for claim {claim_id} is {total_ref.display}"
    if value == paid:
        text += ", which matches the payment record."
    else:
        text += f". The payment record shows {paid_ref.display} paid on this claim, which doesn't match it."
    claim = Claim(text, (total_ref, paid_ref), ())
    return ScanResult("found", text, claim_id, Draft((claim,), ()), own, doc_id, paid, sql)


def _other(
    claim_id: int, doc_id: str, label: str, name: str, field: ScanField | None, own: tuple[ScanField, ...]
) -> ScanResult:
    what = FIELD_LABELS.get(name, name)
    if field is None or field.flagged or field.value is None:
        reasons = plain_reasons(field) if field else f"no {what} was found on the page"
        text = f"The {what} on the scanned {label} for claim {claim_id} couldn't be read reliably: {reasons}."
        return ScanResult("flagged", text, claim_id, Draft((Claim(text, (), ()),), ()), own, doc_id)
    shown = field.value
    if name == "date":
        with suppress(ValueError):
            shown = long_date(date.fromisoformat(field.value))
    text = f"The {what} on the {label} for claim {claim_id} is {shown}."
    return ScanResult("found", text, claim_id, Draft((Claim(text, (), ()),), ()), own, doc_id)
