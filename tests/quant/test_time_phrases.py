from datetime import date

import pytest

from app.semantic.extract import extract
from app.semantic.layer import Layer
from app.semantic.query import MetricQuery, Period

D = date


@pytest.mark.parametrize(
    ("phrase", "start", "end"),
    [
        ("in Q2 2025", D(2025, 4, 1), D(2025, 6, 30)),
        ("for 2025-Q2", D(2025, 4, 1), D(2025, 6, 30)),
        ("in the second quarter of 2025", D(2025, 4, 1), D(2025, 6, 30)),
        ("in the first quarter of 2026", D(2026, 1, 1), D(2026, 3, 31)),
        ("in January 2025", D(2025, 1, 1), D(2025, 1, 31)),
        ("in Sept 2024", D(2024, 9, 1), D(2024, 9, 30)),
        ("in February, 2024", D(2024, 2, 1), D(2024, 2, 29)),
        ("2025", D(2025, 1, 1), D(2025, 12, 31)),
        ("in 2025", D(2025, 1, 1), D(2025, 12, 31)),
        ("during 2024", D(2024, 1, 1), D(2024, 12, 31)),
        ("last quarter", D(2026, 4, 1), D(2026, 6, 30)),
        ("last year", D(2025, 1, 1), D(2025, 12, 31)),
        ("this year", D(2026, 1, 1), D(2026, 6, 30)),
        ("year to date", D(2026, 1, 1), D(2026, 6, 30)),
        ("YTD", D(2026, 1, 1), D(2026, 6, 30)),
        ("last month", D(2026, 6, 1), D(2026, 6, 30)),
        ("between March and May 2025", D(2025, 3, 1), D(2025, 5, 31)),
        ("from November 2024 to February 2025", D(2024, 11, 1), D(2025, 2, 28)),
        ("since January 2026", D(2026, 1, 1), D(2026, 6, 30)),
        ("from 2024 to 2025", D(2024, 1, 1), D(2025, 12, 31)),
        ("over the last 12 months", D(2025, 7, 1), D(2026, 6, 30)),
    ],
)
def test_time_phrase_resolves_against_the_as_of_date(layer: Layer, phrase: str, start: date, end: date) -> None:
    query = extract(f"Paid losses {phrase}", layer)
    assert isinstance(query, MetricQuery)
    assert query.period is not None
    assert (query.period.start, query.period.end) == (start, end)


@pytest.mark.parametrize(
    "question",
    [
        "Paid losses in 2025 versus 2024",
        "Paid losses in 2025 vs 2024",
        "Paid losses in 2025 vs. 2024",
        "Paid losses in 2025 compared to 2024",
        "Paid losses in 2025 year over year",
        "Paid losses in 2025 YoY",
        "Paid losses in 2025 and the year before",
        "Paid losses in 2024 versus 2025",
    ],
)
def test_comparison_phrases_compare_2025_with_2024(layer: Layer, question: str) -> None:
    query = extract(question, layer)
    assert isinstance(query, MetricQuery)
    assert query.period == Period.year(2025)
    assert query.compare_to == "prior_year"


def test_consecutive_quarters_compare_as_prior_period(layer: Layer) -> None:
    query = extract("Claims in Q2 2026 compared to Q1 2026", layer)
    assert isinstance(query, MetricQuery)
    assert (query.period, query.compare_to) == (Period.quarter(2026, 2), "prior_period")


def test_same_quarter_last_year_is_a_year_over_year_comparison(layer: Layer) -> None:
    query = extract("Claims in Q2 2026 versus the same quarter last year", layer)
    assert isinstance(query, MetricQuery)
    assert (query.period, query.compare_to) == (Period.quarter(2026, 2), "prior_year")


def test_unrelated_periods_ask_which_one(layer: Layer) -> None:
    clarify = extract("Claims in 2024 versus Q2 2026", layer)
    assert not isinstance(clarify, MetricQuery)
    assert clarify.missing == "period"
    assert set(clarify.options) == {"2024", "Q2 2026"}


@pytest.mark.parametrize(
    ("phrase", "group_by", "grain"),
    [
        ("by month", (), "month"),
        ("by quarter", (), "quarter"),
        ("by year", (), "year"),
        ("monthly", (), "month"),
        ("by peril", ("peril",), None),
        ("per peril", ("peril",), None),
        ("by state", ("state",), None),
        ("for each region", ("region",), None),
        ("across regions", ("region",), None),
        ("by channel", ("channel",), None),
        ("by region and peril", ("region", "peril"), None),
        ("by state and month", ("state",), "month"),
        ("by cause of loss", ("peril",), None),
    ],
)
def test_grouping_phrases(layer: Layer, phrase: str, group_by: tuple[str, ...], grain: str | None) -> None:
    query = extract(f"Claim count {phrase} in 2025", layer)
    assert isinstance(query, MetricQuery)
    assert (query.group_by, query.grain) == (group_by, grain)


def test_top_n_groups_set_a_limit(layer: Layer) -> None:
    query = extract("Top 3 states by paid losses in 2025", layer)
    assert isinstance(query, MetricQuery)
    assert (query.group_by, query.limit) == (("state",), 3)
