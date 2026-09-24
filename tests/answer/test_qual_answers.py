from datetime import date
from typing import Any

import pytest

from app.answer.qual import NOT_FOUND, ReadingDate, answer_qual, reading_date
from app.config import settings
from app.identity import principal_for
from app.verify import verify
from tests.answer.conftest import dev_cases, visible
from tests.docs.requires import needs_models

pytestmark = pytest.mark.integration

QUALITATIVE = dev_cases("qualitative.jsonl")


@needs_models
@pytest.mark.parametrize("case", QUALITATIVE, ids=[case["id"] for case in QUALITATIVE])
async def test_a_dev_question_is_answered_from_a_relevant_passage_its_user_can_read(case: dict[str, Any]) -> None:
    principal = principal_for(case["user"])
    on_date = date.fromisoformat(case["on_date"]) if case["on_date"] else None
    result = await answer_qual(principal, case["q"], on_date=on_date)
    assert {hit.anchor for hit in result.hits[:5]} & set(case["relevant"]), "no relevant passage in the top 5"
    assert result.kind == "answer" and result.draft.claims
    assert all(claim.citations for claim in result.draft.claims), "a sentence without a citation"
    cited = [chunk_id for claim in result.draft.claims for chunk_id in claim.citations]
    assert await visible(principal, cited) == set(cited)
    assert {hit.anchor for hit in result.hits if hit.chunk_id in cited} & set(case["relevant"])
    assert verify(result.draft, result.evidence, principal).passed


@needs_models
async def test_the_wording_cited_is_the_edition_in_force_on_the_date_asked_about() -> None:
    dana = principal_for("dana")
    question = "What is the wind and hail deductible?"
    before = await answer_qual(dana, question, on_date=date(2024, 6, 1))
    after = await answer_qual(dana, question, on_date=date(2025, 6, 1))
    assert {hit.edition for hit in before.hits} == {"HO-2023"}
    assert {hit.edition for hit in after.hits} == {"HO-2025"}
    assert "$1,000 per occurrence in every region" in before.text
    assert "2% of Coverage A" in after.text
    assert before.draft.caveats == ("Read against HO-2023, the form in force on June 1, 2024.",)


@needs_models
async def test_a_question_the_documents_do_not_cover_gets_no_answer() -> None:
    result = await answer_qual(principal_for("omar"), "What does the policy say about cryptocurrency wallets?")
    assert result.kind == "not_found" and result.text == NOT_FOUND and not result.draft.claims


async def test_a_date_written_in_the_question_is_the_reading_date() -> None:
    reading = await reading_date(principal_for("dana"), "What deductible applied to a hail loss on June 1, 2024?")
    assert reading == ReadingDate(date(2024, 6, 1), "question", "June 1, 2024")


async def test_a_visible_claim_sets_the_reading_date_to_its_loss_date() -> None:
    reading = await reading_date(principal_for("priya"), "What wind deductible applies to claim 100245?")
    assert reading == ReadingDate(date(2024, 2, 14), "claim", "claim 100245")


async def test_a_claim_outside_the_users_regions_is_ignored_without_a_word() -> None:
    reading = await reading_date(principal_for("dana"), "What wind deductible applies to claim 100245?")
    assert reading == ReadingDate(settings().as_of, "today", "")


@pytest.mark.parametrize(
    ("question", "day", "basis"),
    [
        ("What was the mold sublimit on the HO-2023 form?", date(2023, 1, 1), "edition"),
        ("What was the wind and hail deductible under the 2023 form?", date(2023, 1, 1), "edition"),
        ("What did the 2019 form say about mold?", None, "today"),
    ],
)
async def test_a_named_edition_is_read_from_its_first_day(question: str, day: date | None, basis: str) -> None:
    reading = await reading_date(principal_for("omar"), question)
    assert (reading.day, reading.basis) == (day or settings().as_of, basis)
