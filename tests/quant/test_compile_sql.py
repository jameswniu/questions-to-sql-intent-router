import json
from datetime import date

import pytest
import sqlglot
from sqlglot import exp

from app.identity import Principal, principal_for
from app.semantic.compile import Compiled, compile
from app.semantic.layer import VIEWS, Layer
from app.semantic.query import MetricQuery, Period

ADJUSTER = principal_for("dana")
ANALYST = principal_for("sam")
Y2025 = Period.year(2025)


def tables(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql.replace("%s", "NULL"), read="postgres")
    ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
    names = {".".join(p for p in (t.db, t.name) if p) for t in tree.find_all(exp.Table)}
    return names - ctes


def build(layer: Layer, query: MetricQuery, principal: Principal = ADJUSTER) -> Compiled:
    return compile(query, layer, principal)


def sample(layer: Layer, name: str) -> MetricQuery:
    measure = layer.measures[name]
    dims = [d for d in layer.dimensions_for(measure) if d not in ("month", "quarter", "year")]
    period = None if measure.point_in_time else Y2025
    return MetricQuery(
        name, filters={dims[0]: (layer.dimensions[dims[0]].values[0],)}, group_by=(dims[-1],), period=period
    )


@pytest.mark.parametrize(
    "name", ["claim_count", "paid_losses", "claims_paid", "avg_severity", "denial_rate", "loss_ratio", "open_reserve"]
)
def test_every_measure_compiles_to_parameterized_sql_over_semantic_views(layer: Layer, name: str) -> None:
    query = sample(layer, name)
    compiled = build(layer, query)
    assert compiled.kind == "rows"
    assert tables(compiled.sql) <= set(VIEWS) and compiled.relations <= set(VIEWS)
    assert compiled.sql.count("%s") == len(compiled.params)
    assert compiled.sql.rstrip().split()[-2:] == ["LIMIT", "500"]
    assert " ORDER BY " in compiled.sql
    assert set(compiled.provenance) == set(compiled.columns)
    for values in query.filters.values():
        assert all(value not in compiled.sql for value in values)


def test_a_ratio_returns_its_numerator_and_denominator(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("avg_severity", group_by=("peril",), period=Y2025))
    assert compiled.columns == ("peril", "numerator", "denominator", "value")
    assert "numerator::numeric / nullif(denominator, 0) AS value" in compiled.sql


def test_loss_ratio_aggregates_each_side_before_dividing(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("loss_ratio", group_by=("region",), period=Y2025))
    assert tables(compiled.sql) == {"sem.v_payments_net", "sem.v_premium"}
    assert "FULL JOIN den USING (region)" in compiled.sql


def test_a_comparison_reads_both_periods_in_one_statement(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("paid_losses", period=Y2025, compare_to="prior_year"))
    assert len(sqlglot.parse(compiled.sql.replace("%s", "NULL"), read="postgres")) == 1
    assert compiled.columns == ("period", "value")
    assert date(2024, 1, 1) in compiled.params and date(2025, 12, 31) in compiled.params


def test_a_time_split_truncates_the_measure_date(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("claim_count", period=Y2025, grain="quarter"))
    assert "date_trunc('quarter', loss_date)::date AS quarter" in compiled.sql


def test_filter_values_travel_only_as_parameters(layer: Layer) -> None:
    hostile = "hail'); DROP TABLE core.claims; /*"
    compiled = build(layer, MetricQuery("claim_count", filters={"peril": (hostile,)}, period=Y2025))
    assert hostile not in compiled.sql and [hostile] in compiled.params


def test_a_payment_measure_split_by_channel_joins_its_claim(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("paid_losses", group_by=("channel",), period=Y2025))
    assert tables(compiled.sql) == {"sem.v_payments_net", "sem.v_claims"}
    assert compiled.relations == {"sem.v_payments_net", "sem.v_claims"}


def test_a_snapshot_has_no_date_condition(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("open_reserve"))
    assert compiled.params == () and "status = 'open'" in compiled.sql


def test_top_groups_order_by_value(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("paid_losses", group_by=("state",), period=Y2025, limit=3))
    assert compiled.sql.endswith("ORDER BY value DESC NULLS LAST, state LIMIT 3")


def test_an_analyst_query_calls_the_aggregate_function(layer: Layer) -> None:
    query = MetricQuery("claim_count", filters={"peril": ("hail",)}, group_by=("region",), period=Y2025, grain="month")
    compiled = build(layer, query, ANALYST)
    assert compiled.kind == "agg"
    assert compiled.sql == (
        "SELECT grp, num, den, n, suppressed FROM agg.metric(%s, %s::text[], %s::jsonb, %s::date, %s::date) LIMIT 500"
    )
    measure, groups, filters, start, end = compiled.params
    assert (measure, groups, json.loads(filters)) == ("claim_count", ["region", "month"], {"peril": ["hail"]})
    assert (start, end) == (Y2025.start, Y2025.end)
    assert compiled.relations == {"agg.metric"}


def test_an_analyst_comparison_calls_the_function_once_per_period(layer: Layer) -> None:
    compiled = build(layer, MetricQuery("denial_rate", period=Y2025, compare_to="prior_year"), ANALYST)
    assert compiled.sql.count("agg.metric(") == 2
    assert compiled.columns[0] == "period"
    assert compiled.params[3:5] == (Y2025.start, Y2025.end)
    assert compiled.params[8:10] == (date(2024, 1, 1), date(2024, 12, 31))


def test_analysts_cannot_compile_a_snapshot_measure(layer: Layer) -> None:
    with pytest.raises(ValueError, match="not available to analysts"):
        build(layer, MetricQuery("open_reserve"), ANALYST)
