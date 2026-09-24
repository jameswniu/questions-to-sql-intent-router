from decimal import Decimal

import pytest

from app.answer.figures import figures


def read(text: str) -> list[tuple[str, Decimal, str]]:
    return [(w.token, w.value, w.form) for w in figures(text)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Paid $4,108,453 in the quarter.", [("$4,108,453", Decimal(4108453), "dollars")]),
        (
            "A $1,000.50 fee and 4,108,453 dollars.",
            [("$1,000.50", Decimal("1000.5"), "cents"), ("4,108,453 dollars", Decimal(4108453), "dollars")],
        ),
        (
            "About $4.1 million, or 4.1M.",
            [("$4.1 million", Decimal(4100000), "scaled"), ("4.1M", Decimal(4100000), "scaled")],
        ),
        (
            "$412K and 2.5 billion.",
            [("$412K", Decimal(412000), "scaled"), ("2.5 billion", Decimal(2500000000), "scaled")],
        ),
        (
            "Rates of 18.8% and 12.5 percent.",
            [("18.8%", Decimal("18.8"), "percent"), ("12.5 percent", Decimal("12.5"), "percent")],
        ),
        (
            "Up 16.3 points, a 3.2-point drop, 1.0 point, 2 percentage points.",
            [
                ("16.3 points", Decimal("16.3"), "percent"),
                ("3.2-point", Decimal("3.2"), "percent"),
                ("1.0 point", Decimal(1), "percent"),
                ("2 percentage points", Decimal(2), "percent"),
            ],
        ),
        (
            "An r squared of 0.81 over 1,001 claims in 12 months.",
            [("0.81", Decimal("0.81"), "decimal"), ("1,001", Decimal(1001), "integer"), ("12", Decimal(12), "integer")],
        ),
    ],
)
def test_each_way_of_writing_a_quantity_is_read(text: str, expected: list[tuple[str, Decimal, str]]) -> None:
    assert read(text) == expected


def test_a_written_minus_is_kept_and_a_range_dash_is_not_a_sign() -> None:
    (loss,) = figures("A net change of -$1,234 this year.")
    assert loss.negative and loss.value == 1234
    low, high = figures("Between $5-$7 a claim.")
    assert not low.negative and not high.negative


@pytest.mark.parametrize(
    "text",
    [
        "Paid losses in 2025 against 2024, and 2024 to 2026.",
        "Q2 2025 against Q1 2025, H1 2026 and FY2025.",
        "The loss on June 30, 2026 was reported 30 June 2026 and closed Jan. 9.",
        "Lost on 2026-06-30, booked in 2025-04, paid 7/9/2026 at 9:30.",
        "Claim 100013 and claims 100171, 100357 or 100592.",
        "Claim #100013 on policy 5000001, claim number 100592.",
        "Read against the HO-2025 wording, not HO-2023.",
        "Section 4.2 and § 7 of the wording [3] apply.",
        "See ho-2025#5-3-deductible-schedule:1 for the 2nd time.",
    ],
)
def test_labels_that_carry_digits_are_not_read_as_quantities(text: str) -> None:
    assert figures(text) == ()


def test_a_bare_year_is_a_label_but_a_count_with_a_separator_is_read() -> None:
    assert read("In 2025 there were 2,025 claims.") == [("2,025", Decimal(2025), "integer")]


def test_a_long_number_without_separators_is_an_identifier() -> None:
    assert read("File 106951 shows 250 claims.") == [("250", Decimal(250), "integer")]


def test_the_token_is_quoted_from_the_original_text() -> None:
    (written,) = figures("Reserve on claim 100013 was $12,500 on HO-2025.")
    assert written.token == "$12,500"
