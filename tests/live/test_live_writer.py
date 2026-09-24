import asyncio
import time
from typing import NoReturn

import pytest

from app.answer.types import Claim, Draft, Evidence
from app.identity import principal_for
from app.live import support, writer
from app.live.context import passage
from app.live.errors import Invalid, Unread
from app.live.support import Readings
from app.live.writer import WHY_WRITING, Writer, to_claims, write_qual
from app.llm.client import Response
from app.llm.fake import ScriptedLLM, cited, json_reply, reply
from app.llm.request import Request
from app.verify import COULD_NOT_CONFIRM, verify
from tests.live.conftest import MEMO, make_hit

WORDING = make_hit(
    "ho-2025#section-3:1",
    "Section 3 covers a sudden and accidental discharge of water from a plumbing system. "
    "The all peril deductible is $1,000.",
    kind="wording",
    title="Form HO-2025",
)
PRIYA = principal_for("priya")
EVIDENCE = Evidence((), (WORDING,), (), ())
KEPT = Claim("Burst pipes are covered under Section 3.", (), (WORDING.chunk_id,))
STRETCHED = Claim("Burst pipes are covered with no deductible.", (), (WORDING.chunk_id,))
SUPPORTED = json_reply({"supported": True, "reason": "Section 3 says so."})
UNSUPPORTED = json_reply({"supported": False, "reason": "The text gives a $1,000 deductible."})
WORDS = ("one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
DEDUCTIBLE = "The all peril deductible is $1,000."
HAIL = "For hail it is $2,500."
PLUMBING = "Section 3 covers water from a plumbing system."
REFUSED = reply("", stop_reason="refusal")


def written(*sentences: str) -> Response:
    """A reply citing the wording for every sentence."""
    return reply(*(cited(f" {sentence}", (WORDING.chunk_id, 0)) for sentence in sentences))


async def hang(request: Request) -> NoReturn:
    await asyncio.sleep(30)
    raise AssertionError("a deadline should have stopped this call")


def test_the_reply_becomes_one_claim_per_cited_sentence_naming_the_chunk_ids_in_it() -> None:
    passages = (passage(MEMO), passage(WORDING))
    response = reply(
        "According to the hail memo, ",
        cited("a hailstorm damaged roofs across Colorado", (MEMO.chunk_id, 0)),
        ". This was a very bad year for everyone.",
        " The deductible is $2,500.",
        cited(" Burst pipes are covered.", (WORDING.chunk_id, 1), (WORDING.chunk_id, 1)),
        cited(" It says so.", ("memo-99#made-up:1", 0)),
    )
    assert [(c.text, c.numbers, c.citations) for c in to_claims(response, passages)] == [
        ("According to the hail memo, a hailstorm damaged roofs across Colorado.", (), (MEMO.chunk_id,)),
        ("Burst pipes are covered.", (), (WORDING.chunk_id,)),
        ("It says so.", (), ("unmatched:memo-99#made-up:1",)),
    ]


async def test_a_written_answer_that_cites_nothing_is_invalid() -> None:
    llm = ScriptedLLM(reply("I couldn't find that in the documents."))
    with pytest.raises(Invalid):
        await write_qual(llm, PRIYA, "Is mold covered?", EVIDENCE)


async def test_a_written_answer_gets_one_retry_with_the_verifiers_reasons_before_anything_is_cut() -> None:
    llm = ScriptedLLM(
        written(KEPT.text, DEDUCTIBLE, HAIL, STRETCHED.text),
        # Three readings: the $2,500 sentence fails the figure check first, so it is never read.
        SUPPORTED,
        SUPPORTED,
        UNSUPPORTED,
        written(KEPT.text, DEDUCTIBLE, PLUMBING),
        # Only the new sentence is read; the other two were read the first time.
        SUPPORTED,
    )
    done = await write_qual(llm, PRIYA, "Is a burst pipe covered?", EVIDENCE, caveats=lambda hits: ("Read it.",))
    first, second = [r for r in llm.requests if r.template != support.TEMPLATE]
    assert (first.template, second.template) == (writer.QUAL_TEMPLATE, writer.QUAL_RETRY_TEMPLATE)
    assert first.model == second.model == llm.main_model and not first.tools and not second.tools
    # The second writing keeps the cached system prompt, and gets the verifier's reasons for what didn't hold.
    assert first.system == second.system and first.params()["system"][-1]["cache_control"] == {"type": "ephemeral"}
    [feedback] = [part for part in second.messages[0].parts if isinstance(part, str) and "didn't hold up" in part]
    assert f'"{HAIL}": figure' in feedback and f'"{STRETCHED.text}": reading' in feedback
    assert KEPT.text not in feedback and llm.left == 0 and done.retried
    assert done.answer.text == f"{KEPT.text} {DEDUCTIBLE} {PLUMBING} Read it."
    assert not done.answer.claims_cut and done.answer.citations == (WORDING,)
    assert [claim.text for claim in done.draft.claims] == [KEPT.text, DEDUCTIBLE, PLUMBING]
    assert verify(done.draft, EVIDENCE, PRIYA, second_check=done.second_check).passed


async def test_what_still_fails_after_the_retry_is_cut() -> None:
    llm = ScriptedLLM(written(KEPT.text, STRETCHED.text), SUPPORTED, UNSUPPORTED, written(KEPT.text, STRETCHED.text))
    done = await write_qual(llm, PRIYA, "Is a burst pipe covered?", EVIDENCE)
    assert done.retried and llm.left == 0
    assert done.answer.text == KEPT.text and [claim.text for claim in done.answer.claims_cut] == [STRETCHED.text]
    assert done.answer.could_not_confirm == (COULD_NOT_CONFIRM["reading"],)


async def test_a_second_writing_that_fails_leaves_the_first_standing() -> None:
    llm = ScriptedLLM(written(KEPT.text, STRETCHED.text), SUPPORTED, UNSUPPORTED, REFUSED)
    done = await write_qual(llm, PRIYA, "Is a burst pipe covered?", EVIDENCE)
    assert done.retried and llm.left == 0
    assert done.answer.text == KEPT.text and [claim.text for claim in done.answer.claims_cut] == [STRETCHED.text]


async def test_a_second_writing_the_budget_has_no_room_for_is_never_asked_for() -> None:
    llm = ScriptedLLM(written(KEPT.text, STRETCHED.text), SUPPORTED, UNSUPPORTED)
    # The budget left as the writing starts, as its reading starts, and when the verifier asks for a second writing.
    remaining = iter([10.0, 9.0, writer.VERIFY_MARGIN_S / 2]).__next__
    draft_writer = Writer(llm, PRIYA, "Is a burst pipe covered?", (WORDING,), writer.QUAL_WRITING, remaining=remaining)
    answer = await draft_writer.answer(EVIDENCE)
    assert not draft_writer.retried and llm.left == 0 and answer.text == KEPT.text


@pytest.mark.parametrize("sentence", [HAIL, STRETCHED.text])
async def test_a_written_answer_nothing_of_which_holds_leaves_the_extractive_one_standing(sentence: str) -> None:
    # The figure check cuts the $2,500 sentence before any reading; the reading cuts the other.
    readings = [] if sentence == HAIL else [UNSUPPORTED]
    llm = ScriptedLLM(written(sentence), *readings, written(sentence))
    with pytest.raises(Unread, match="none of the written sentences held"):
        await write_qual(llm, PRIYA, "What is the deductible?", EVIDENCE)
    assert llm.left == 0


async def test_only_so_many_written_sentences_are_kept_and_read() -> None:
    many = [f"Burst pipes are covered, note {word}." for word in WORDS]
    llm = ScriptedLLM(written(*many), *(SUPPORTED for _ in range(9)))
    done = await write_qual(llm, PRIYA, "Is a burst pipe covered?", EVIDENCE)
    assert len(done.draft.claims) == len(llm.sent(support.TEMPLATE)) == writer.MAX_QUAL_CLAIMS == 4
    causes = ScriptedLLM(written(*many), *(SUPPORTED for _ in range(9)))
    why_writer = Writer(causes, PRIYA, "Why?", (WORDING,), WHY_WRITING, remaining=lambda: 10.0)
    assert len((await why_writer(EVIDENCE, ())).claims) == writer.MAX_CAUSES == 3
    assert len(causes.sent(support.TEMPLATE)) == 3
    # Whatever a caller hands it, the reading reads no more than its cap and counts the rest as unread.
    claims = tuple(Claim(sentence, (), (WORDING.chunk_id,)) for sentence in many)
    reader, readings = ScriptedLLM(*(SUPPORTED for _ in range(9))), Readings()
    await readings.read(reader, PRIYA, claims, EVIDENCE, deadline_s=5.0)
    assert len(reader.sent(support.TEMPLATE)) == support.MAX_READINGS == 4
    cited = support.cited_text((WORDING,))
    assert [readings(claim, cited) for claim in claims] == [True] * 4 + [False] * 5


async def test_the_live_reading_cuts_a_claim_its_cited_text_doesnt_bear_out() -> None:
    llm, readings = ScriptedLLM(SUPPORTED, UNSUPPORTED), Readings()
    await readings.read(llm, PRIYA, (KEPT, STRETCHED), EVIDENCE, deadline_s=5.0)
    verification = verify(Draft((KEPT, STRETCHED), ()), EVIDENCE, PRIYA, second_check=readings)
    assert [c.supported for c in verification.checks] == [True, False]
    assert verification.checks[1].reasons == ("reading: the cited text doesn't bear the claim out",)
    for request in llm.requests:
        body = request.params()
        assert request.template == support.TEMPLATE and request.model == llm.fast_model and not request.tools
        assert [p.source for p in request.passages] == [WORDING.chunk_id]
        assert body["messages"][0]["content"][0]["citations"] == {"enabled": False}
    # A claim or cited text it never read counts as unsupported.
    assert not readings(KEPT, "some other text")


async def test_a_reading_that_fails_leaves_the_draft_unread() -> None:
    with pytest.raises(Unread, match="didn't run"):
        await Readings().read(ScriptedLLM(REFUSED), PRIYA, (KEPT,), EVIDENCE, deadline_s=5.0)


async def test_a_written_answer_stops_at_its_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await write_qual(ScriptedLLM(hang), PRIYA, "Is a burst pipe covered?", EVIDENCE, deadline_s=0.3)
    with pytest.raises(Unread, match="didn't run"):
        await write_qual(ScriptedLLM(written(KEPT.text), hang), PRIYA, "Covered?", EVIDENCE, deadline_s=0.3)
    assert time.monotonic() - started < 5
