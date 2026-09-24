"""Which questions reach the qualitative answer path. answer_qual can only answer what the router sends it, and a
question sent to the figures or to a clarify gets no passage at all, so the routing rules are pinned here beside
the figure, forecast and out-of-range questions they must leave alone."""

from typing import Any

import pytest

from app.route import NOTE_REQUEST, route
from tests.answer.conftest import dev_cases

QUALITATIVE = dev_cases("qualitative.jsonl")


@pytest.mark.parametrize("case", QUALITATIVE, ids=[case["id"] for case in QUALITATIVE])
def test_every_dev_qualitative_question_is_routed_to_the_documents(case: dict[str, Any]) -> None:
    assert route(case["q"]).route == case["route"] == "qualitative"


@pytest.mark.parametrize(
    "question",
    [
        # A year that names an edition points at a wording, which is there whatever years the data covers.
        "What did the 2023 edition say about mold?",
        "Is theft covered under the HO 2023 wording?",
        # A form named by its number is the wording, as the words form, edition and policy already are.
        "What does HO-2023 say about roof surfaces?",
        # What someone should or must do is a guideline, even when it names a measure such as a payment.
        "What must an adjuster record when a payment is voided?",
        "What should we do when a reserve is above our authority?",
        # The data holds no durations, so how soon something happens is a rule in the documents.
        "How quickly do we send a denial letter?",
        # An operational event is recorded in memos, not in the claims data.
        "When was the payments outage in May 2026?",
        # Adjuster notes are documents, whichever claims or perils they are about.
        "What did the adjusters note about hail damage in Colorado?",
        "Any file notes mentioning a roof replacement?",
    ],
)
def test_a_question_about_what_the_documents_say_is_routed_to_them(question: str) -> None:
    assert route(question).route == "qualitative"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # A claim with a wording question goes to the wording, read on the claim's loss date.
        ("What wind deductible applies to claim 100245?", "qualitative"),
        ("Is the water damage on claim #104512 covered under the HO-2023 form?", "qualitative"),
        ("Which edition applies to claim 103422?", "qualitative"),
        # A claim's notes are documents, so they go to the documents, which keep that claim's notes alone.
        ("What do the adjuster notes say about claim 103388?", "qualitative"),
        ("Has anyone noted roof damage on claim #103388?", "qualitative"),
        # Anything else about a claim is its record: status, its policyholder, its scanned documents.
        ("What's the status of claim 100245?", "lookup"),
        ("Who is the policyholder on claim 100245?", "lookup"),
        ("What's the deductible on the proof of loss for claim 103254?", "lookup"),
    ],
)
def test_a_claim_question_goes_to_the_wording_only_when_it_asks_about_the_wording(question: str, expected: str) -> None:
    assert route(question).route == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How many claims did we have in 2019?", "out_of_data"),
        ("Claims by region in 2023", "out_of_data"),
        ("Paid losses for the 2023 policy year", "out_of_data"),
        ("How many claims should we expect next year?", "out_of_data"),
        ("Paid losses in the West in Q2 2025", "quantitative"),
        ("How many claims had a payment in May 2026?", "quantitative"),
        ("What are our open reserves?", "quantitative"),
        ("Why were paid losses in the West so high in Q2 2025?", "why"),
    ],
)
def test_figures_forecasts_and_years_outside_the_data_keep_their_routes(question: str, expected: str) -> None:
    assert route(question).route == expected


@pytest.mark.parametrize(
    ("question", "is_lookup"),
    [
        ("What's the status of claim 100245?", True),
        ("Show me #100245", True),
        ("How many claims 2025?", False),
        ("What's the status of claim 10024?", False),
    ],
)
def test_only_a_six_digit_number_reads_as_a_claim(question: str, is_lookup: bool) -> None:
    # Claim ids have six digits, the same rule the lookup itself uses, so a year after "claims" stays a year.
    assert (route(question).route == "lookup") is is_lookup


@pytest.mark.parametrize(
    ("question", "asks"),
    [
        ("What do the adjuster notes say about claim 103670?", True),
        ("What have adjusters noted about ice dams this winter?", True),
        # Naming the wording or the policy asks what the wording says, so the wording answers, not the notes.
        ("Is the wind damage noted on claim 100245 covered?", False),
        ("What is noted in the policy about claim 100245?", False),
    ],
)
def test_only_a_question_for_notes_that_names_no_wording_is_answered_from_notes(question: str, asks: bool) -> None:
    assert (NOTE_REQUEST.search(question) is not None) is asks
