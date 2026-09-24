from datetime import date

import pytest

from app.answer.mentions import claim_id_in, date_in, edition_in, issued_on


@pytest.mark.parametrize(
    ("text", "claim_id"),
    [
        ("What's the status of claim 100245?", 100245),
        ("show me claim #104512", 104512),
        ("the invoice on claim number 100013", 100013),
        ("#103388 notes", 103388),
        ("How many claims in 2025?", None),
    ],
)
def test_a_claim_is_found_by_its_number(text: str, claim_id: int | None) -> None:
    assert claim_id_in(text) == claim_id


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("What deductible applied to a hail loss in Colorado on June 1, 2024?", date(2024, 6, 1)),
        ("a loss on Sept 3rd 2025", date(2025, 9, 3)),
        ("read it as of 2024-06-01", date(2024, 6, 1)),
        ("loss dated 6/1/2024", date(2024, 6, 1)),
        ("What did the April 2025 hail bulletin say?", None),
        ("the loss on February 30, 2025", None),
    ],
)
def test_only_a_whole_calendar_day_counts_as_a_date(text: str, day: date | None) -> None:
    assert date_in(text) == day


@pytest.mark.parametrize(
    ("text", "edition"),
    [
        ("What was the mold sublimit on the HO-2023 form?", "HO-2023"),
        ("under HO 2025", "HO-2025"),
        ("What was the wind and hail deductible under the 2023 form?", "HO-2023"),
        ("the 2025 edition", "HO-2025"),
        ("paid losses in 2025", None),
    ],
)
def test_an_edition_is_named_by_form_or_year(text: str, edition: str | None) -> None:
    assert edition_in(text) == edition


def test_a_memo_or_bulletin_dates_itself_on_its_issued_or_date_line() -> None:
    assert issued_on("Issued May 9, 2025 by Catastrophe Response.") == date(2025, 5, 9)
    assert issued_on("To: West staff\nDate: October 15, 2025\nRe: CL-25-14") == date(2025, 10, 15)
    assert issued_on("Two days of storms on April 12 and 13, 2025.") is None
