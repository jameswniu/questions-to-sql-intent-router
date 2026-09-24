from dataclasses import replace

import pytest

from app.semantic.extract import extract
from app.semantic.layer import Layer
from app.semantic.query import Clarify, MetricQuery, Period


def query_of(question: str, layer: Layer, previous: MetricQuery | None = None) -> MetricQuery:
    result = extract(question, layer, previous)
    assert isinstance(result, MetricQuery), result
    return result


def test_every_layer_example_extracts_exactly(layer: Layer) -> None:
    for example in layer.examples:
        assert extract(example.q, layer) == example.query, example.q


def test_a_question_with_no_measure_offers_three_measures(layer: Layer) -> None:
    clarify = extract("What happened in the West in 2025?", layer)
    assert isinstance(clarify, Clarify)
    assert clarify.missing == "measure"
    assert len(clarify.options) == 3
    for option in clarify.options:
        assert query_of(f"{option} in 2025", layer).measure in layer.measures


def test_a_measure_with_no_period_offers_three_periods_that_extract(layer: Layer) -> None:
    clarify = extract("How many claims?", layer)
    assert isinstance(clarify, Clarify)
    assert clarify.missing == "period"
    assert clarify.options == ("2025", "Q2 2026", "year to date")
    previous = MetricQuery("claim_count")
    for option in clarify.options:
        assert query_of(option, layer, previous).period is not None


def test_a_snapshot_measure_never_asks_for_a_period(layer: Layer) -> None:
    query = query_of("What are our open reserves?", layer)
    assert (query.measure, query.period) == ("open_reserve", None)


@pytest.mark.parametrize(
    ("question", "measure"),
    [
        ("How much did we pay on hail claims in 2025?", "paid_losses"),
        ("Average severity for hail claims in 2025", "avg_severity"),
        ("How many claims had a payment in May 2026?", "claims_paid"),
        ("Denial rate for wind claims in 2025", "denial_rate"),
        ("How many wind claims in 2025?", "claim_count"),
    ],
)
def test_a_specific_measure_outranks_the_bare_word_claims(layer: Layer, question: str, measure: str) -> None:
    assert query_of(question, layer).measure == measure


def test_filters_use_synonyms_and_full_state_names(layer: Layer) -> None:
    query = query_of("Paid losses on burst pipe claims in Texas and western hailstorms in 2025", layer)
    assert query.filters == {"peril": ("water", "hail"), "state": ("TX",), "region": ("West",)}


def test_state_codes_match_only_in_capitals(layer: Layer) -> None:
    assert query_of("Claims in OK in 2025", layer).filters == {"state": ("OK",)}
    assert query_of("ok, claims in 2025", layer).filters == {}


def test_a_negated_value_keeps_the_rest_of_its_dimension(layer: Layer) -> None:
    query = query_of("Ignore open claims and give me paid losses in the West for 2025", layer)
    assert query.measure == "paid_losses"
    assert query.filters == {"status": ("closed", "denied"), "region": ("West",)}


def test_an_unrecognised_qualifier_is_kept_for_resolution(layer: Layer) -> None:
    assert query_of("How many hurricane claims in 2025?", layer).unknown == ("hurricane",)


def test_ordinary_words_before_claims_are_not_qualifiers(layer: Layer) -> None:
    assert query_of("How many total claims did we have in 2025?", layer).unknown == ()


def test_a_follow_up_period_carries_the_rest(layer: Layer) -> None:
    first = query_of("Paid losses for hail claims in Colorado in Q2 2025", layer)
    assert query_of("and in Q3?", layer, first) == replace(first, period=Period.quarter(2025, 3))


def test_a_follow_up_value_replaces_its_own_dimension(layer: Layer) -> None:
    first = query_of("Paid losses for wind claims in the West in 2025", layer)
    follow = query_of("what about hail?", layer, first)
    assert follow.filters == {"peril": ("hail",), "region": ("West",)}
    assert (follow.measure, follow.period) == (first.measure, first.period)


def test_a_follow_up_region_drops_a_state_outside_it(layer: Layer) -> None:
    first = query_of("Claims in Colorado in 2025", layer)
    assert query_of("and the East?", layer, first).filters == {"region": ("East",)}


def test_a_follow_up_can_change_the_measure(layer: Layer) -> None:
    first = query_of("Paid losses in the West in 2025 versus 2024", layer)
    follow = query_of("what about the denial rate?", layer, first)
    assert follow == replace(first, measure="denial_rate")


def test_a_new_complete_question_does_not_inherit_filters(layer: Layer) -> None:
    first = query_of("Paid losses in the West in 2025", layer)
    assert query_of("How many claims in 2024?", layer, first).filters == {}


def test_a_year_less_quarter_without_context_asks_which_year(layer: Layer) -> None:
    clarify = extract("Paid losses in Q3", layer)
    assert isinstance(clarify, Clarify)
    assert clarify.options == ("Q3 2025", "Q3 2024")


def test_a_reply_to_a_period_question_completes_the_first_question(layer: Layer) -> None:
    clarify = extract("How many hail claims in Texas?", layer)
    assert isinstance(clarify, Clarify) and clarify.partial is not None
    query = query_of("2025", layer, clarify.partial)
    assert (query.measure, query.filters, query.period) == (
        "claim_count",
        {"peril": ("hail",), "state": ("TX",)},
        Period.year(2025),
    )


def test_a_capitalised_place_the_vocabulary_lacks_is_kept_for_resolution(layer: Layer) -> None:
    assert query_of("Paid losses in Nevada in 2025", layer).unknown == ("Nevada",)
    assert query_of("Paid losses in May 2025", layer).unknown == ()


def test_hyphenated_phrases_match_like_spaced_ones(layer: Layer) -> None:
    assert query_of("Paid losses on water-damage claims in 2025", layer).filters == {"peril": ("water",)}
