from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from app.answer import drivers as dv
from app.answer.format import Format, NumberRef, change_refs
from app.answer.types import Claim, Draft, Evidence
from app.answer.why import _driver_claim
from app.identity import Principal, principal_for
from app.sandbox import templates
from app.sandbox.client import Table
from app.semantic.layer import Layer
from app.semantic.query import MetricQuery, Period
from app.verify import verify

Q2 = Period.quarter(2025, 2)


def decompose_here(table: Table, dim: str) -> dict[str, Any]:
    return templates.decompose(pd.DataFrame(table.rows, columns=table.columns), dv.decompose_params(dim))


def split_of(dim: str, records: list[dict[str, Any]], value: str, count: str) -> dv.Split:
    rows = [[r["period"], r[dim], r[value], r[count]] for r in records]
    at = {(r["period"], r[dim]): i for i, r in enumerate(records)}
    return dv.Split(dim, Table(["period", dim, "value", "n"], rows), records, [], at)


def headline(records: list[dict[str, Any]], ratio: bool) -> dict[str, dv.Side]:
    columns = tuple(records[0])
    return dv.sides(dv.Fetched(columns, tuple(tuple(r.values()) for r in records), ""), ratio)


def headline_change(sides: dict[str, dv.Side], source: str, fmt: Format) -> tuple[NumberRef, ...]:
    """The headline's change refs, which a driver sentence carries for the rise or fall it names."""
    now, then = sides["current"], sides["prior"]
    assert now.value is not None and then.value is not None
    return change_refs(now.value, then.value, fmt, (now.row, then.row), source)


PAID = [{"period": "current", "value": Decimal("1000.00")}, {"period": "prior", "value": Decimal("400.00")}]
BY_PERIL = [
    {"period": "current", "peril": "hail", "value": Decimal("800.00"), "n": 8},
    {"period": "current", "peril": "wind", "value": Decimal("200.00"), "n": 2},
    {"period": "prior", "peril": "hail", "value": Decimal("150.00"), "n": 3},
    {"period": "prior", "peril": "wind", "value": Decimal("250.00"), "n": 2},
]
DENIALS = [
    {"period": "current", "numerator": 30, "denominator": 100, "value": Decimal("0.3")},
    {"period": "prior", "numerator": 10, "denominator": 100, "value": Decimal("0.1")},
]
BY_REGION = [
    {"period": "current", "region": "West", "numerator": 20, "denominator": 50},
    {"period": "current", "region": "South", "numerator": 10, "denominator": 50},
    {"period": "prior", "region": "West", "numerator": 5, "denominator": 50},
    {"period": "prior", "region": "South", "numerator": 5, "denominator": 50},
]


def test_group_parts_of_a_sum_add_up_to_its_change() -> None:
    sides = headline(PAID, ratio=False)
    split = split_of("peril", BY_PERIL, "value", "n")
    groups = dv.contributions(
        decompose_here(split.table, "peril"),
        split,
        2,
        ratio=False,
        agg=False,
        current=sides["current"],
        prior=sides["prior"],
    )
    assert sum(g.contribution for g in groups) == Decimal(600)
    assert {g.key: g.share for g in groups} == {"hail": Decimal("650") / 600, "wind": Decimal("-50") / 600}


def test_group_parts_of_a_ratio_add_up_to_the_ratio_change() -> None:
    sides = headline(DENIALS, ratio=True)
    split = split_of("region", BY_REGION, "numerator", "denominator")
    groups = dv.contributions(
        decompose_here(split.table, "region"),
        split,
        2,
        ratio=True,
        agg=False,
        current=sides["current"],
        prior=sides["prior"],
    )
    assert sum(g.contribution for g in groups) == Decimal("0.2")
    assert {g.key: g.share for g in groups} == {"West": Decimal("0.75"), "South": Decimal("0.25")}


@pytest.mark.parametrize(
    ("ratio", "headline_rows", "records", "source", "fmt"),
    [(False, PAID, BY_PERIL, "value", "currency"), (True, DENIALS, BY_REGION, "numerator / denominator", "percent")],
)
def test_the_verifier_recomputes_each_share_from_the_rows(
    ratio: bool, headline_rows: list[dict[str, Any]], records: list[dict[str, Any]], source: str, fmt: Format
) -> None:
    dim = "region" if ratio else "peril"
    sides = headline(headline_rows, ratio)
    split = split_of(dim, records, "numerator" if ratio else "value", "denominator" if ratio else "n")
    result = decompose_here(split.table, dim)
    groups = dv.contributions(result, split, 2, ratio=ratio, agg=False, current=sides["current"], prior=sides["prior"])
    change = headline_change(sides, source, fmt)
    claims = tuple(Claim(f"It accounts for {g.ref.display} of the rise.", (g.ref, *change), ()) for g in groups)
    evidence = Evidence((*headline_rows, *records), (), (result,), ())
    checks = verify(Draft(claims, ()), evidence, principal_for("priya")).checks
    assert all(check.supported for check in checks), [check.reasons for check in checks]


def test_the_rise_or_fall_a_driver_sentence_names_is_checked_against_the_headline_change() -> None:
    sides = headline(PAID, ratio=False)
    split = split_of("peril", BY_PERIL, "value", "n")
    result = decompose_here(split.table, "peril")
    groups = dv.contributions(result, split, 2, ratio=False, agg=False, current=sides["current"], prior=sides["prior"])
    hail = next(g for g in groups if g.key == "hail")
    change = headline_change(sides, "value", "currency")
    evidence = Evidence((*PAID, *BY_PERIL), (), (result,), ())

    def checked(claim: Claim) -> tuple[str, ...]:
        (check,) = verify(Draft((claim,), ()), evidence, principal_for("priya")).checks
        return check.reasons

    written = _driver_claim([(hail,)], True, change)
    assert written.text == "Hail claims account for 108.3% of the rise." and checked(written) == ()
    fall = _driver_claim([(hail,)], False, change)
    assert checked(fall) == ('direction: "fall" goes with the change the claim traces, which is a rise in the data',)
    # The share alone doesn't say which way the total moved.
    assert checked(Claim(written.text, (hail.ref,), ())) == ('direction: "rise" isn\'t traced to a change in the data',)


def agg(period: str, region: str | None, num: int | None, den: int | None) -> dict[str, Any]:
    """A row as the aggregate function returns it. A suppressed cell has num, den and n all None."""
    return {
        "period": period,
        "grp": {"region": region} if region else {},
        "num": None if num is None else Decimal(num),
        "den": None if den is None else Decimal(den),
        "n": den,
        "suppressed": num is None,
    }


AGG_HEADLINE = [agg("current", None, 60, 200), agg("prior", None, 20, 200)]
# In the aggregate function's order, by period then group, with East suppressed in the current period.
AGG_BY_REGION = [
    agg("current", "East", None, None),
    agg("current", "North", 10, 60),
    agg("current", "West", 40, 100),
    agg("prior", "East", 5, 40),
    agg("prior", "North", 5, 60),
    agg("prior", "West", 10, 100),
]


async def test_the_verifier_accepts_a_visible_share_after_a_suppressed_group(
    monkeypatch: pytest.MonkeyPatch, layer: Layer
) -> None:
    async def by_region(principal: Principal, mq: MetricQuery, layer: Layer) -> dv.Fetched:
        return dv.Fetched(tuple(AGG_BY_REGION[0]), tuple(tuple(r.values()) for r in AGG_BY_REGION), "")

    monkeypatch.setattr(dv, "fetch", by_region)
    analyst = principal_for("sam")
    split = await dv.group_table(analyst, MetricQuery("denial_rate", period=Q2), "region", layer)
    sides = headline(AGG_HEADLINE, ratio=True)
    result = decompose_here(split.table, "region")
    groups = dv.contributions(
        result, split, len(AGG_HEADLINE), ratio=True, agg=True, current=sides["current"], prior=sides["prior"]
    )
    west = next(g for g in groups if g.key == "West")
    assert west.share == Decimal("0.75")
    change = headline_change(sides, "num / den", "percent")
    claim = Claim(f"West accounts for {west.ref.display} of the rise.", (west.ref, *change), ())
    evidence = Evidence((*AGG_HEADLINE, *split.records), (), (result,), ())
    [check] = verify(Draft((claim,), ()), evidence, analyst).checks
    assert check.supported, check.reasons


def group(dim: str, key: str, share: str) -> dv.Group:
    return dv.Group(dim, key, Decimal(share), Decimal(share), "")


def test_one_group_covering_most_of_the_change_is_the_driver_alone() -> None:
    assert dv.top_groups([group("peril", "hail", "0.8"), group("peril", "wind", "0.2")]) == (
        group("peril", "hail", "0.8"),
    )


def test_two_groups_are_named_when_neither_covers_most_alone() -> None:
    top = dv.top_groups(
        [group("region", "West", "0.5"), group("region", "South", "0.48"), group("region", "East", "0.02")]
    )
    assert [g.key for g in top] == ["West", "South"]


def test_a_second_dimension_is_named_only_when_it_also_covers_most() -> None:
    peril = [group("peril", "hail", "0.97"), group("peril", "wind", "0.03")]
    state = [group("state", "CO", "0.96"), group("state", "AZ", "0.04")]
    weak = [group("channel", "web", "0.4"), group("channel", "app", "0.35"), group("channel", "phone", "0.25")]
    assert [[g.key for g in top] for top in dv.pick_drivers([peril, state])] == [["hail"], ["CO"]]
    assert [[g.key for g in top] for top in dv.pick_drivers([peril, weak])] == [["hail"]]


def test_a_dimension_with_one_group_explains_nothing() -> None:
    assert dv.pick_drivers([[group("region", "North", "1")]]) == []


def test_a_pinned_dimension_is_not_split_again(layer: Layer) -> None:
    colorado_hail = MetricQuery("avg_severity", {"state": ("CO",), "peril": ("hail",)}, period=Q2)
    assert dv.free_dimensions(colorado_hail, layer, analyst=False) == []
    west = MetricQuery("paid_losses", {"region": ("West",)}, period=Q2)
    assert dv.free_dimensions(west, layer, analyst=False) == ["peril", "state"]
    wind = MetricQuery("denial_rate", {"peril": ("wind",)}, period=Q2)
    assert dv.free_dimensions(wind, layer, analyst=False) == ["region", "state"]


def test_premium_measures_split_only_by_region(layer: Layer) -> None:
    assert dv.free_dimensions(MetricQuery("loss_ratio", period=Q2), layer, analyst=False) == ["region"]
