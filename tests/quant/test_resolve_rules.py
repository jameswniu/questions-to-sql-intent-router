from dataclasses import replace
from datetime import date

import pytest

from app.semantic.layer import Layer
from app.semantic.query import Clarify, MetricQuery, OutOfData, Period
from app.semantic.resolve import resolve


def ok(result: MetricQuery | Clarify | OutOfData) -> MetricQuery:
    assert isinstance(result, MetricQuery), result
    return result


def vocabulary(layer: Layer) -> set[str]:
    return {value for dim in layer.dimensions.values() for value in dim.values}


@pytest.mark.parametrize("period", [Period.year(2019), Period.year(2027), Period.quarter(2026, 3)])
def test_a_period_outside_the_data_names_the_window(layer: Layer, period: Period) -> None:
    result = resolve(MetricQuery("claim_count", period=period), layer)
    assert isinstance(result, OutOfData)
    assert result.covered == layer.coverage
    assert "January 2024 to June 2026" in result.reason


def test_a_period_partly_outside_the_data_is_clipped_and_noted(layer: Layer) -> None:
    query = ok(resolve(MetricQuery("paid_losses", period=Period.year(2026)), layer))
    assert query.period == Period.between(date(2026, 1, 1), date(2026, 6, 30))
    assert query.notes == ("My data covers January 2024 to June 2026, so this is 2026 through June.",)


def test_a_comparison_reaching_before_the_data_is_out_of_data(layer: Layer) -> None:
    result = resolve(MetricQuery("paid_losses", period=Period.year(2024), compare_to="prior_year"), layer)
    assert isinstance(result, OutOfData)
    assert "2023" in result.reason


def test_an_unknown_value_gets_near_matches(layer: Layer) -> None:
    result = resolve(MetricQuery("claim_count", filters={"peril": ("hial",)}, period=Period.year(2025)), layer)
    assert isinstance(result, Clarify)
    assert result.options[0] == "hail"


def test_values_are_matched_without_regard_to_case(layer: Layer) -> None:
    query = ok(resolve(MetricQuery("claim_count", filters={"region": ("west",)}, period=Period.year(2025)), layer))
    assert query.filters == {"region": ("West",)}


@pytest.mark.parametrize("word", ["hurricane", "earthquake", "zzzz", "Dallas", "web portal", "Mitchell"])
def test_suggestions_come_only_from_the_layer_vocabulary(layer: Layer, word: str) -> None:
    result = resolve(MetricQuery("claim_count", period=Period.year(2025), unknown=(word,)), layer)
    assert isinstance(result, Clarify)
    assert result.options and set(result.options) <= vocabulary(layer)


def test_loss_ratio_grouped_by_peril_asks_for_another_split(layer: Layer) -> None:
    result = resolve(MetricQuery("loss_ratio", group_by=("peril",), period=Period.year(2025)), layer)
    assert isinstance(result, Clarify)
    assert result.options == ("region", "month", "quarter", "year")


def test_loss_ratio_filtered_by_peril_asks_too(layer: Layer) -> None:
    result = resolve(MetricQuery("loss_ratio", filters={"peril": ("hail",)}, period=Period.year(2025)), layer)
    assert isinstance(result, Clarify)


def test_loss_ratio_by_region_resolves(layer: Layer) -> None:
    ok(resolve(MetricQuery("loss_ratio", group_by=("region",), period=Period.year(2025)), layer))


def test_a_snapshot_drops_any_period_with_a_note(layer: Layer) -> None:
    query = ok(resolve(MetricQuery("open_reserve", period=Period.year(2025)), layer))
    assert query.period is None and query.notes


def test_a_snapshot_cannot_be_split_by_time(layer: Layer) -> None:
    assert isinstance(resolve(MetricQuery("open_reserve", grain="month"), layer), Clarify)


def test_a_comparison_with_a_time_split_asks_which(layer: Layer) -> None:
    query = MetricQuery("paid_losses", period=Period.year(2025), compare_to="prior_year", grain="month")
    assert isinstance(resolve(query, layer), Clarify)


def test_a_missing_period_asks_with_options(layer: Layer) -> None:
    result = resolve(MetricQuery("claim_count"), layer)
    assert isinstance(result, Clarify) and len(result.options) == 3


def test_analysts_cannot_filter_by_status(layer: Layer) -> None:
    query = MetricQuery("claim_count", filters={"status": ("open",)}, period=Period.year(2025))
    assert isinstance(resolve(query, layer, analyst=True), Clarify)
    ok(resolve(query, layer))


def test_analyst_periods_widen_to_whole_months(layer: Layer) -> None:
    period = Period.between(date(2025, 3, 10), date(2025, 5, 20))
    query = ok(resolve(MetricQuery("claim_count", period=period), layer, analyst=True))
    assert query.period == Period.between(date(2025, 3, 1), date(2025, 5, 31))
    assert query.notes == ("This figure is kept by whole months, so it covers March to May 2025.",)


def test_an_oversized_limit_is_capped(layer: Layer) -> None:
    query = MetricQuery("claim_count", group_by=("state",), period=Period.year(2025), limit=10_000)
    assert ok(resolve(query, layer)).limit == 50


def test_resolving_twice_changes_nothing(layer: Layer) -> None:
    once = ok(resolve(MetricQuery("paid_losses", period=Period.year(2026)), layer))
    assert ok(resolve(replace(once, notes=()), layer)).period == once.period


def test_a_blocked_split_keeps_the_rest_of_the_question_for_the_reply(layer: Layer) -> None:
    query = MetricQuery("loss_ratio", filters={"region": ("West",)}, group_by=("peril",), period=Period.year(2025))
    result = resolve(query, layer)
    assert isinstance(result, Clarify)
    assert result.partial == replace(query, group_by=())
