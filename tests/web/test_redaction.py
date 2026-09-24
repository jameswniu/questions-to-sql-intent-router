import pytest

from app.redact import redact, sql_for_span


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("Show me claim 100245", "Show me claim [number]"),
        ("Email dana.reyes+work@example.co.uk about it", "Email [email] about it"),
        ("SSN 123-45-6789 is on file", "SSN [ssn] is on file"),
        ("SSN 123456789 is on file", "SSN [number] is on file"),
        ("Call (612) 555-0142 or 612.555.0142", "Call [phone] or [phone]"),
    ],
)
def test_redact_masks_long_numbers_emails_ssns_and_phones(raw: str, masked: str) -> None:
    assert redact(raw) == masked


def test_redact_keeps_the_wording_and_short_numbers() -> None:
    question = "Paid losses in the West for Q2 2025, over $1,000 per claim, top 10 by state"
    assert redact(question) == question


def test_span_sql_keeps_placeholders_and_masks_inline_literals() -> None:
    statement = "SELECT claim_id FROM sem.v_claims WHERE state = 'CO' AND reserve > 1000.5 AND claim_id = %s LIMIT 5"
    assert sql_for_span(statement) == (
        "SELECT claim_id FROM sem.v_claims WHERE state = %s AND reserve > %s AND claim_id = %s LIMIT %s"
    )


def test_span_sql_is_dropped_when_it_does_not_parse() -> None:
    assert sql_for_span("not sql at all ;; drop") is None
