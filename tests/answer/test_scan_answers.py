from dataclasses import asdict
from decimal import Decimal
from typing import Any

import pytest

from app.answer.lookup import ANALYST_TEXT
from app.answer.mentions import claim_id_in
from app.answer.scanfield import answer_scan, plain_reasons
from app.answer.types import Evidence
from app.identity import principal_for
from app.sources.scans import ScanField
from app.verify import verify
from tests.answer.conftest import dev_cases

OCR = dev_cases("ocr.jsonl")


def spellings(raw: str) -> set[str]:
    digits = raw.replace("$", "").replace(",", "")
    try:
        return {raw, digits, f"{Decimal(digits):,.2f}"}
    except ArithmeticError:
        return {raw, digits}


@pytest.mark.integration
@pytest.mark.parametrize("case", OCR, ids=[case["id"] for case in OCR])
async def test_a_dev_scan_question_states_a_clean_total_or_falls_back_to_the_payment_record(
    case: dict[str, Any],
) -> None:
    principal = principal_for(case["user"])
    claim_id = claim_id_in(case["q"])
    assert claim_id is not None
    result = await answer_scan(principal, case["q"], claim_id)
    assert result.doc_id == case["scan"]
    total = next((f for f in result.fields if f.field == "total"), None)
    if total is None or total.flagged or total.value is None:
        assert result.kind == "flagged"
        assert "couldn't be read reliably" in result.text and "The payment record shows $" in result.text
        if total is not None and total.value:
            assert not any(spelling in result.text for spelling in spellings(total.value))
    else:
        assert result.kind == "found" and result.text.endswith("which matches the payment record.")
    evidence = Evidence(({"paid": result.paid},), (), (), tuple(asdict(f) for f in result.fields))
    assert verify(result.draft, evidence, principal).passed


@pytest.mark.integration
@pytest.mark.parametrize(
    ("question", "asked"),
    [
        ("What's the total on the invoice for claim 104967?", "total"),
        ("Who is the vendor on the invoice for claim 104967?", "vendor"),
    ],
)
async def test_the_field_the_answer_is_about_leads_the_scan_evidence(question: str, asked: str) -> None:
    # The evidence panel draws a scan's first six fields, and this invoice has eight.
    result = await answer_scan(principal_for("priya"), question, 104967)
    assert len(result.fields) > 6
    assert result.fields[0].field == asked


@pytest.mark.integration
async def test_a_clean_total_is_stated_with_its_match_to_the_payment_record() -> None:
    result = await answer_scan(principal_for("dana"), "What's the total on the invoice for claim 100013?", 100013)
    assert result.text == "The total on the invoice for claim 100013 is $17,333.71, which matches the payment record."


@pytest.mark.integration
async def test_a_flagged_total_is_never_stated_and_the_payment_record_stands_in() -> None:
    result = await answer_scan(principal_for("priya"), "What's the total on the invoice for claim 100171?", 100171)
    assert result.kind == "flagged"
    assert not any(s in result.text for s in spellings("5936.32"))
    assert result.text == (
        "The scanned total on the invoice for claim 100171 couldn't be read reliably: it doesn't match what was paid"
        " on the claim and the line items don't add up to it. The payment record shows $9,536.32 paid on this claim."
    )


@pytest.mark.integration
async def test_a_claim_outside_the_users_region_reads_exactly_like_one_that_does_not_exist() -> None:
    june = principal_for("june")
    outside = await answer_scan(june, "What's the total on the invoice for claim 100013?", 100013)
    missing = await answer_scan(june, "What's the total on the invoice for claim 999999?", 999999)
    assert (outside.kind, outside.text) == (missing.kind, missing.text.replace("999999", "100013"))
    assert outside.text == "I can't find an invoice for claim 100013." and not outside.fields


@pytest.mark.integration
async def test_an_analyst_cannot_open_a_claims_scans() -> None:
    result = await answer_scan(principal_for("sam"), "What's the total on the invoice for claim 100013?", 100013)
    assert (result.kind, result.text) == ("not_allowed", ANALYST_TEXT)


def test_flag_reasons_read_as_plain_words() -> None:
    field = ScanField("scan-1", "Proof of loss", "total", "$5,38@85", 43.6, None, True, "low confidence, format")
    assert plain_reasons(field) == (
        "the text was too faint or blurred to read with confidence and what was read isn't in the form a total takes"
    )
    missing = ScanField("scan-1", "Proof of loss", "total", None, None, None, True, "missing")
    assert plain_reasons(missing) == "no total was found on the page"
