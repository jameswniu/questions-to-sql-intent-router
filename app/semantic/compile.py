import json
from dataclasses import dataclass, field
from typing import Any, Literal

from app.identity import Principal
from app.semantic.layer import CLAIM_VIEW, VIEWS, Layer, Measure
from app.semantic.query import MetricQuery, Period

ROW_LIMIT = 500
AGG_FUNCTION = "agg.metric"
AGG_COLUMNS = ("grp", "num", "den", "n", "suppressed")


@dataclass(frozen=True)
class Compiled:
    sql: str
    params: tuple[Any, ...]
    kind: Literal["rows", "agg"]
    columns: tuple[str, ...]
    provenance: dict[str, str]
    relations: frozenset[str] = frozenset()


@dataclass
class _Side:
    """One aggregated read of one view: its select list, filters and the parameters they bind, in order."""

    view: str
    date: str
    select: list[str] = field(default_factory=list)
    select_params: list[Any] = field(default_factory=list)
    where: list[str] = field(default_factory=list)
    where_params: list[Any] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    joined: list[str] = field(default_factory=list)

    def source(self) -> str:
        if not self.joined:
            return self.view
        own = ", ".join(f"p.{column}" for column in sorted(VIEWS[self.view]))
        extra = ", ".join(f"c.{column}" for column in self.joined)
        return f"(SELECT {own}, {extra} FROM {self.view} AS p JOIN {CLAIM_VIEW} AS c ON c.claim_id = p.claim_id) AS f"

    def sql(self) -> str:
        text = f"SELECT {', '.join(self.select)} FROM {self.source()} WHERE {' AND '.join(self.where)}"
        if self.keys:
            text += " GROUP BY " + ", ".join(str(i) for i in range(1, len(self.keys) + 1))
        return text

    def params(self) -> list[Any]:
        return [*self.select_params, *self.where_params]


def _column(layer: Layer, side: _Side, dim: str) -> str:
    column = layer.dimensions[dim].column
    assert column is not None
    if column not in VIEWS[side.view] and column not in side.joined:
        side.joined.append(column)
    return column


def _side(view: str, date: str, mq: MetricQuery, layer: Layer, where: str | None) -> _Side:
    assert mq.period is not None
    side = _Side(view, date)
    periods = [mq.period]
    if mq.compare_to is not None:
        prior = mq.period.years_earlier() if mq.compare_to == "prior_year" else mq.period.prior()
        periods.append(prior)
        side.select.append(f"CASE WHEN {date} BETWEEN %s AND %s THEN 'current' ELSE 'prior' END AS period")
        side.select_params += [mq.period.start, mq.period.end]
        side.keys.append("period")
    for dim in mq.group_by:
        side.select.append(f"{_column(layer, side, dim)} AS {dim}")
        side.keys.append(dim)
    if mq.grain is not None:
        side.select.append(f"date_trunc('{mq.grain}', {date})::date AS {mq.grain}")
        side.keys.append(mq.grain)
    side.where.append("(" + " OR ".join(f"{date} BETWEEN %s AND %s" for _ in periods) + ")")
    side.where_params += [bound for p in periods for bound in (p.start, p.end)]
    if where:
        side.where.append(f"({where})")
    for dim, values in mq.filters.items():
        side.where.append(f"{_column(layer, side, dim)} = ANY(%s)")
        side.where_params.append(list(values))
    return side


def _order(mq: MetricQuery, keys: list[str]) -> str:
    if mq.limit is not None:
        return " ORDER BY value DESC NULLS LAST, " + ", ".join(keys) if keys else " ORDER BY value DESC NULLS LAST"
    return " ORDER BY " + ", ".join(keys) if keys else ""


def _describe(period: Period, prior: Period | None) -> str:
    return period.label if prior is None else f"{period.label}, and {prior.label} for the prior rows"


def _provenance(mq: MetricQuery, measure: Measure, layer: Layer, keys: list[str]) -> dict[str, str]:
    assert mq.period is not None
    prior = None
    if mq.compare_to is not None:
        prior = mq.period.years_earlier() if mq.compare_to == "prior_year" else mq.period.prior()
    where = f" where {measure.where}" if measure.where else ""
    span = _describe(mq.period, prior)
    notes: dict[str, str] = {}
    for key in keys:
        if key == "period":
            notes[key] = f"current is {mq.period.label}, prior is {prior.label if prior else ''}"
        elif key == mq.grain:
            notes[key] = f"{key} of {measure.sources[0]}.{measure.dates[0]}"
        else:
            notes[key] = f"{measure.sources[0]}.{layer.dimensions[key].column}"
    if measure.kind == "ratio":
        notes["numerator"] = f"{measure.numerator} over {measure.sources[0]}{where}, {span}"
        notes["denominator"] = (
            f"{measure.denominator} over {measure.sources[-1]}{where if len(measure.sources) == 1 else ''}"
        )
        notes["value"] = "numerator / denominator"
    else:
        notes["value"] = f"{measure.sql} over {measure.sources[0]}{where}, {span}"
    return notes


def _rows(mq: MetricQuery, measure: Measure, layer: Layer) -> Compiled:
    limit = f" LIMIT {mq.limit or ROW_LIMIT}"
    if measure.point_in_time:
        side = _Side(measure.sources[0], "")
        for dim in mq.group_by:
            side.select.append(f"{_column(layer, side, dim)} AS {dim}")
            side.keys.append(dim)
        side.select.append(f"{measure.sql} AS value")
        side.where = [f"({measure.where})"] if measure.where else ["true"]
        for dim, values in mq.filters.items():
            side.where.append(f"{_column(layer, side, dim)} = ANY(%s)")
            side.where_params.append(list(values))
        keys = list(side.keys)
        sql = side.sql() + _order(mq, keys) + limit
        notes = {key: f"{measure.sources[0]}.{layer.dimensions[key].column}" for key in keys}
        notes["value"] = f"{measure.sql} over {measure.sources[0]} where {measure.where}, as of the data date"
        return Compiled(sql, tuple(side.params()), "rows", (*keys, "value"), notes, frozenset(_views(side)))
    if measure.kind != "ratio":
        side = _side(measure.sources[0], measure.dates[0], mq, layer, measure.where)
        side.select.append(f"{measure.sql} AS value")
        keys = list(side.keys)
        sql = side.sql() + _order(mq, keys) + limit
        return Compiled(
            sql,
            tuple(side.params()),
            "rows",
            (*keys, "value"),
            _provenance(mq, measure, layer, keys),
            frozenset(_views(side)),
        )
    assert measure.numerator is not None and measure.denominator is not None
    if len(measure.sources) == 1:
        side = _side(measure.sources[0], measure.dates[0], mq, layer, measure.where)
        side.select += [f"{measure.numerator} AS numerator", f"{measure.denominator} AS denominator"]
        keys, params, views = list(side.keys), side.params(), _views(side)
        head, body = "", f"FROM ({side.sql()}) AS t"
    else:
        # Numerator and denominator come from different views, so each is aggregated on its own and joined on the keys.
        num = _side(measure.sources[0], measure.dates[0], mq, layer, measure.where)
        den = _side(measure.sources[1], measure.dates[1], mq, layer, None)
        num.select.append(f"{measure.numerator} AS numerator")
        den.select.append(f"{measure.denominator} AS denominator")
        keys, params, views = list(num.keys), [*num.params(), *den.params()], _views(num) | _views(den)
        head = f"WITH num AS ({num.sql()}), den AS ({den.sql()}) "
        body = "FROM num " + (f"FULL JOIN den USING ({', '.join(keys)})" if keys else "CROSS JOIN den")
    shown = [*keys, "numerator", "denominator"]
    sql = (
        f"{head}SELECT {', '.join(shown)}, numerator::numeric / nullif(denominator, 0) AS value "
        f"{body}{_order(mq, keys)}{limit}"
    )
    notes = _provenance(mq, measure, layer, keys)
    return Compiled(sql, tuple(params), "rows", (*shown, "value"), notes, frozenset(views))


def _views(side: _Side) -> set[str]:
    return {side.view, CLAIM_VIEW} if side.joined else {side.view}


def _agg_call(mq: MetricQuery, period: Period) -> tuple[str, list[Any]]:
    groups = [*mq.group_by, *([mq.grain] if mq.grain else [])]
    filters = json.dumps({dim: list(values) for dim, values in mq.filters.items()}, sort_keys=True)
    sql = f"FROM {AGG_FUNCTION}(%s, %s::text[], %s::jsonb, %s::date, %s::date)"
    return sql, [mq.measure, groups, filters, period.start, period.end]


def _agg(mq: MetricQuery) -> Compiled:
    assert mq.period is not None
    columns = ", ".join(AGG_COLUMNS)
    notes = {
        "grp": "the group, as the aggregate function labels it",
        "num": f"numerator from {AGG_FUNCTION}('{mq.measure}'), withheld when the cell is too small",
        "den": "denominator, for ratios",
        "n": "distinct claims in the cell",
        "suppressed": "true when the cell was withheld",
    }
    source, params = _agg_call(mq, mq.period)
    order = " ORDER BY num DESC NULLS LAST" if mq.limit is not None else ""
    relations = frozenset({AGG_FUNCTION})
    if mq.compare_to is None:
        sql = f"SELECT {columns} {source}{order} LIMIT {mq.limit or ROW_LIMIT}"
        return Compiled(sql, tuple(params), "agg", AGG_COLUMNS, notes, relations)
    prior = mq.period.years_earlier() if mq.compare_to == "prior_year" else mq.period.prior()
    prior_source, prior_params = _agg_call(mq, prior)
    # The checker accepts a single SELECT, so the union sits in a subquery.
    sql = (
        f"SELECT period, {columns} FROM (SELECT 'current' AS period, {columns} {source} UNION ALL "
        f"SELECT 'prior' AS period, {columns} {prior_source}) AS both_periods ORDER BY period, grp LIMIT {ROW_LIMIT}"
    )
    notes["period"] = f"current is {mq.period.label}, prior is {prior.label}"
    return Compiled(sql, (*params, *prior_params), "agg", ("period", *AGG_COLUMNS), notes, relations)


def compile(mq: MetricQuery, layer: Layer, principal: Principal) -> Compiled:
    measure = layer.measures[mq.measure]
    if principal.kind == "analyst":
        if not measure.analyst:
            raise ValueError(f"{measure.name} is not available to analysts")
        return _agg(mq)
    return _rows(mq, measure, layer)
