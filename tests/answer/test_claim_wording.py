"""A wording question that names a claim is answered from the policy wording in force on that claim's loss date.
The claim is read as the asker, so a claim they can't open gets the reply lookup gives for it, word for word."""

from collections.abc import AsyncIterator

import pytest

from app import events as ev
from app import pipeline
from app.answer.lookup import ANALYST_TEXT
from app.answer.qual import answer_qual
from app.answer.types import Answer
from app.config import Backend
from app.identity import Principal, principal_for
from app.memory import Memory
from app.requestlog import RequestRecord
from app.web.stream import answer_stream
from tests.docs.requires import needs_models

pytestmark = pytest.mark.integration

# Claim 100245 is a Texas wind loss of February 14, 2024 (HO-2023); 103422 a Colorado hail loss of April 12, 2025
# (HO-2025). Priya supervises every region; Dana adjusts the West only; Sam is an analyst.
WIND_100245 = "What wind deductible applies to claim 100245?"
HAIL_103422 = "What hail deductible applies to claim 103422?"


async def ask(user: str, question: str) -> tuple[Answer, ev.Done]:
    events = [event async for event in pipeline.ask(principal_for(user), question, "s1", memory=Memory())]
    done = events[-1]
    assert isinstance(done, ev.Done)
    return next(e for e in events if isinstance(e, Answer)), done


async def recorded(user: str, question: str) -> RequestRecord:
    """The request_log row the web route would write for the question, kept here instead of written."""
    rows: list[RequestRecord] = []

    async def ask_fn(
        principal: Principal, question: str, session_id: str, *, backend: Backend = "none"
    ) -> AsyncIterator[object]:
        async for event in pipeline.ask(principal, question, session_id, backend=backend, memory=Memory()):
            yield event

    async def keep(row: RequestRecord) -> None:
        rows.append(row)

    stream = answer_stream(ask_fn, principal_for(user), question, "s1", backend="none", record=keep, source="eval")
    assert [item async for item in stream]
    [row] = rows
    return row


@needs_models
@pytest.mark.parametrize(
    ("question", "edition", "says", "when"),
    [
        (WIND_100245, "HO-2023", "$1,000 per occurrence in every region", "claim 100245, February 14, 2024"),
        (HAIL_103422, "HO-2025", "2% of Coverage A", "claim 103422, April 12, 2025"),
    ],
)
async def test_the_edition_in_force_on_the_claims_loss_date_answers(
    question: str, edition: str, says: str, when: str
) -> None:
    answer, done = await ask("priya", question)
    assert (done.route, done.outcome) == ("qualitative", "answer")
    assert answer.claims_kept and not answer.claims_cut
    # Only that edition's wording: not the other form, and not a memo about a later change.
    assert {(hit.kind, hit.edition) for hit in answer.citations} == {("wording", edition)}
    assert says in answer.text
    assert f"Read against {edition}, the form in force on the loss date of {when}." in answer.text


@needs_models
async def test_a_question_naming_no_peril_is_read_for_the_claims_own_peril_and_region() -> None:
    # Both are January 2, 2025 losses under HO-2025, so only the peril and region differ: 102455 is wind in the
    # West, where the 2% wind and hail deductible applies, and 102456 is water in the North, where it doesn't.
    wind, wind_done = await ask("priya", "What deductible applies to claim 102455?")
    water, water_done = await ask("priya", "What deductible applies to claim 102456?")
    assert wind_done.outcome == water_done.outcome == "answer"
    assert {c.text for c in wind.claims_kept}.isdisjoint(c.text for c in water.claims_kept)
    assert "2% of Coverage A" in wind.text
    assert "$1,000" in water.text and "2%" not in water.text
    assert all("the form in force on the loss date of claim" in a.text for a in (wind, water))


@pytest.mark.parametrize("claim", [100245, 999999])
async def test_a_claim_the_asker_cannot_open_reads_as_lookup_does(claim: int) -> None:
    # 100245 is outside Dana's region and 999999 doesn't exist: both must read the same, as they do in lookup.
    wording, wording_done = await ask("dana", f"What wind deductible applies to claim {claim}?")
    status, status_done = await ask("dana", f"What's the status of claim {claim}?")
    assert (wording_done.route, status_done.route) == ("qualitative", "lookup")
    assert wording.text == status.text
    assert wording_done.outcome == status_done.outcome == "not_found"
    assert not wording.citations and not wording_done.claim_ids and not wording_done.doc_ids


async def test_an_analyst_gets_the_lookup_reply_for_a_claim() -> None:
    result = await answer_qual(principal_for("sam"), WIND_100245)
    assert (result.kind, result.text) == ("not_allowed", ANALYST_TEXT)
    assert not result.hits and not result.draft.claims
    wording, wording_done = await ask("sam", WIND_100245)
    status, status_done = await ask("sam", "What's the status of claim 100245?")
    assert wording.text == status.text == ANALYST_TEXT
    assert wording_done.outcome == status_done.outcome == "not_allowed"


async def test_the_request_log_records_the_refusal_lookup_would() -> None:
    wording = await recorded("sam", WIND_100245)
    status = await recorded("sam", "What's the status of claim 100245?")
    assert (wording.route, wording.outcome) == ("qualitative", "not_allowed")
    assert (status.route, status.outcome) == ("lookup", "not_allowed")
    # A claim outside the asker's regions is not_found, as lookup records it, never not_allowed.
    assert (await recorded("dana", WIND_100245)).outcome == "not_found"
