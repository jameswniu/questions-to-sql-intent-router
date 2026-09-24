import pytest

from app.answer.types import Answer, Claim, Draft, Evidence
from app.identity import principal_for
from app.verify import COULD_NOT_CONFIRM, NO_ANSWER, finalize, verify, verify_and_retry
from tests.verify.builders import PAID_TEXT, draft, evidence, hit, paid_claim, paid_refs

DANA = principal_for("dana")
WRONG = paid_claim(PAID_TEXT.replace("$35,768,928", "$35,768,982"))
UNSOURCED = paid_claim(citations=("memo#gone:1",))
DEADLINE = Claim("The guide sets an inspection deadline.", (), ("guide#wind:1",))


def answer_for(proposed: Draft, found: Evidence | None = None) -> Answer:
    found = found or evidence()
    return finalize(proposed, verify(proposed, found, DANA))


def test_finalize_keeps_what_holds_and_says_plainly_what_was_cut() -> None:
    answer = answer_for(draft(paid_claim(), WRONG))
    assert answer.text == PAID_TEXT
    assert answer.claims_kept == (paid_claim(),) and answer.claims_cut == (WRONG,)
    assert answer.could_not_confirm == (COULD_NOT_CONFIRM["figure"],)
    assert "35,768,982" not in " ".join((answer.text, *answer.could_not_confirm))


def test_each_cut_claim_gets_a_line_worded_by_what_failed() -> None:
    answer = answer_for(draft(paid_claim(), WRONG, UNSOURCED))
    assert answer.could_not_confirm == (COULD_NOT_CONFIRM["figure"], COULD_NOT_CONFIRM["source"])


def test_when_every_claim_is_cut_the_answer_says_it_could_not_confirm_one() -> None:
    answer = answer_for(draft(WRONG, caveats=("This covers the West.",)))
    assert answer.text == NO_ANSWER == "I couldn't confirm an answer from the data and documents available."
    assert answer.claims_kept == () and len(answer.could_not_confirm) == 1


def test_bullets_join_on_new_lines_sentences_on_spaces_and_caveats_come_last() -> None:
    bullet = Claim("- 2025: $35,768,928", paid_refs()[:1], ())
    grouped = draft(paid_claim(), bullet, caveats=("Groups marked withheld aren't shown.",))
    assert answer_for(grouped).text == f"{PAID_TEXT}\n- 2025: $35,768,928\nGroups marked withheld aren't shown."
    scalar = draft(paid_claim(), caveats=("This covers the West.",))
    assert answer_for(scalar).text == f"{PAID_TEXT} This covers the West."


def test_the_answer_cites_each_hit_its_kept_claims_cite_once() -> None:
    found = evidence(hits=[hit("guide#wind:1"), hit("guide#hail:1")])
    cited = draft(
        paid_claim(citations=("guide#wind:1",)), paid_claim(citations=("guide#wind:1", "guide#hail:1")), UNSOURCED
    )
    assert [h.chunk_id for h in answer_for(cited, found).citations] == ["guide#wind:1", "guide#hail:1"]


def test_finalize_refuses_a_verification_of_another_draft() -> None:
    with pytest.raises(ValueError, match="different draft"):
        finalize(draft(paid_claim()), verify(draft(WRONG), evidence(), DANA))


class ScriptedComposer:
    """Returns its drafts in order, repeating the last, and keeps the feedback it was given."""

    def __init__(self, *drafts: Draft) -> None:
        self.drafts = drafts
        self.feedback: list[tuple[str, ...]] = []

    def __call__(self, found: Evidence, feedback: tuple[str, ...]) -> Draft:
        self.feedback.append(feedback)
        return self.drafts[min(len(self.feedback), len(self.drafts)) - 1]


async def test_a_failing_draft_is_composed_again_with_its_reasons() -> None:
    compose = ScriptedComposer(draft(paid_claim(), WRONG), draft(paid_claim()))
    answer = await verify_and_retry(compose, evidence(), DANA)
    reason = "figure: $35,768,982 matches none of the claim's numbers (closest is $35,768,928)"
    assert compose.feedback == [(), (f'"{WRONG.text}": {reason}',)]
    assert answer.claims_cut == () and answer.text == PAID_TEXT


async def test_a_deterministic_composer_makes_the_retry_a_no_op() -> None:
    same = draft(paid_claim(), WRONG)
    compose = ScriptedComposer(same)
    answer = await verify_and_retry(compose, evidence(), DANA)
    assert len(compose.feedback) == 2
    assert answer == finalize(same, verify(same, evidence(), DANA))


async def test_a_draft_that_passes_is_composed_once() -> None:
    compose = ScriptedComposer(draft(paid_claim()))
    await verify_and_retry(compose, evidence(), DANA)
    assert compose.feedback == [()]


async def test_an_async_composer_is_awaited() -> None:
    async def compose(found: Evidence, feedback: tuple[str, ...]) -> Draft:
        return draft(paid_claim())

    assert (await verify_and_retry(compose, evidence(), DANA)).text == PAID_TEXT


async def test_the_live_check_runs_on_the_first_draft_and_the_retry() -> None:
    doubted: list[str] = []

    def doubt(claim: Claim, cited: str) -> bool:
        doubted.append(claim.text)
        return False

    found = evidence(hits=[hit("guide#wind:1")])
    answer = await verify_and_retry(ScriptedComposer(draft(DEADLINE)), found, DANA, second_check=doubt)
    assert doubted == [DEADLINE.text, DEADLINE.text]
    assert answer.text == NO_ANSWER and answer.could_not_confirm == (COULD_NOT_CONFIRM["reading"],)
