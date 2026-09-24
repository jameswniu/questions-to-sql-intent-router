from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from functools import cache
from typing import Any

import yaml

from app import db
from app.answer.format import NumberRef, as_decimal, format_ratio
from app.answer.quant import verify_sql
from app.identity import Principal
from app.sandbox.client import Sandbox, Table
from app.semantic.compile import compile
from app.semantic.describe import state_regions
from app.semantic.layer import LAYER_PATH, Layer
from app.semantic.query import MetricQuery

DEFAULT_BREAKDOWN = ("peril", "state")
# One group covering this share of the change is the driver on its own; below it, the top two together must.
MAJORITY = Decimal("0.6")
# A second dimension's driver is worth naming beside the first only when it covers at least this share.
SECOND_DRIVER = Decimal("0.5")

# Takes the login role, the table and the template's params. The sandbox daemon runs it in production;
# tests pass the template function itself.
Decompose = Callable[[str, Table, dict[str, Any]], Awaitable[dict[str, Any]]]
# A statement as it ran: its SQL, and the values bound to its %s placeholders in order.
Ran = tuple[str, tuple[Any, ...]]


class SandboxFailed(RuntimeError):
    pass


@cache
def driver_specs() -> dict[str, Any]:
    with LAYER_PATH.open() as fh:
        return dict(yaml.safe_load(fh).get("drivers") or {})


async def in_sandbox(role: str, table: Table, params: dict[str, Any]) -> dict[str, Any]:
    result = await Sandbox().run(role, template="decompose", code=None, params=params, table=table)
    if not result.ok or result.result is None:
        raise SandboxFailed(result.error or result.killed or f"the sandbox answered {result.status}")
    return result.result


@dataclass(frozen=True)
class Fetched:
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    sql: str
    params: tuple[Any, ...] = ()
    # The measure the rows hold. A sum's split fetches the count it is split over as well, and both come back in a
    # column named value, so each record names its measure.
    measure: str | None = None

    def records(self) -> list[dict[str, Any]]:
        named = {"measure": self.measure} if self.measure else {}
        return [named | dict(zip(self.columns, row, strict=True)) for row in self.rows]

    @property
    def ran(self) -> Ran:
        return self.sql, self.params


@dataclass(frozen=True)
class Side:
    """One period of the headline figure: its value, its numerator and denominator, and the row it came from."""

    value: Decimal | None
    num: Decimal | None
    den: Decimal | None
    row: int


@dataclass(frozen=True)
class Group:
    dim: str
    key: str
    contribution: Decimal
    share: Decimal
    # The share as arithmetic on evidence row cells, which the verifier recomputes.
    derivation: str

    @property
    def ref(self) -> NumberRef:
        return NumberRef(self.share, format_ratio(self.share), "share", None, self.derivation)


@dataclass(frozen=True)
class Split:
    """One dimension's table for the sandbox, the result rows it was built from, the statements that fetched them,
    and where each group's rows sit."""

    dim: str
    table: Table
    records: list[dict[str, Any]]
    ran: list[Ran]
    at: dict[tuple[str, str], int]


async def fetch(principal: Principal, mq: MetricQuery, layer: Layer) -> Fetched:
    compiled = compile(mq, layer, principal)
    sql = verify_sql(compiled.sql, relations=compiled.relations)
    found = await db.run(principal, sql, compiled.params)
    return Fetched(tuple(found.columns), tuple(found.rows), sql, compiled.params, mq.measure)


def sides(fetched: Fetched, ratio: bool) -> dict[str, Side]:
    """The current and prior headline figures, from either the row path or the aggregate function."""
    out: dict[str, Side] = {}
    for i, record in enumerate(fetched.records()):
        period = str(record.get("period", "current"))
        if "grp" in record:
            num, den = _decimal(record["num"]), _decimal(record["den"])
            if record["suppressed"] or num is None:
                out[period] = Side(None, None, None, i)
            else:
                out[period] = Side((num / den if den else None) if ratio else num, num, den, i)
        elif ratio:
            num, den = _decimal(record["numerator"]), _decimal(record["denominator"])
            out[period] = Side(_decimal(record["value"]), num, den, i)
        else:
            value = _decimal(record["value"])
            out[period] = Side(value, value, None, i)
    return out


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else as_decimal(value)


def free_dimensions(mq: MetricQuery, layer: Layer, *, analyst: bool) -> list[str]:
    """Dimensions left to split the change by: the measure's breakdown plus region, minus any the question pins."""
    measure = layer.measures[mq.measure]
    breakdown = driver_specs().get(mq.measure, {}).get("breakdown") or DEFAULT_BREAKDOWN
    reachable = layer.dimensions_for(measure, analyst=analyst)
    pinned_regions = {state_regions()[s] for s in mq.filters.get("state", ())} | set(mq.filters.get("region", ()))
    free = []
    for dim in dict.fromkeys(["region", *breakdown]):
        if dim not in reachable or len(mq.filters.get(dim, ())) == 1:
            continue
        if dim == "region" and len(pinned_regions) == 1:
            continue
        free.append(dim)
    return free


async def group_table(principal: Principal, mq: MetricQuery, dim: str, layer: Layer) -> Split:
    """Per group and period: the value to split and the count it is split over."""
    kind = layer.measures[mq.measure].kind
    split = replace(mq, group_by=(dim,), grain=None, limit=None, compare_to="prior_period")
    main = await fetch(principal, split, layer)
    fetched = [main]
    counts: dict[tuple[str, Any], Any] = {}
    count_measure = driver_specs().get(mq.measure, {}).get("count")
    analyst = principal.kind == "analyst"
    if not analyst and kind == "sum" and count_measure:
        extra = await fetch(principal, replace(split, measure=count_measure), layer)
        fetched.append(extra)
        counts = {(str(r["period"]), r[dim]): r["value"] for r in extra.records()}
    rows, at = [], {}
    for index, record in enumerate(main.records()):
        period = str(record["period"])
        if analyst:
            if record["suppressed"] or record["num"] is None:
                continue
            key = (record["grp"] or {}).get(dim)
            value, n = record["num"], record["den"] if kind == "ratio" else record["n"]
        else:
            key = record[dim]
            if kind == "ratio":
                value, n = record["numerator"], record["denominator"]
            else:
                value = record["value"]
                n = value if kind == "count" else counts.get((period, key), 1)
        rows.append([period, str(key), value, n])
        # Suppressed rows stay in the evidence (records, below), so a group's evidence row is its index in main,
        # not its place in the sandbox table.
        at[(period, str(key))] = index
    records = [record for f in fetched for record in f.records()]
    return Split(dim, Table(["period", dim, "value", "n"], rows), records, [f.ran for f in fetched], at)


async def whole_table(principal: Principal, mq: MetricQuery, layer: Layer, current: Side, prior: Side) -> Split:
    """Both periods as one group, for a question that pins every dimension there is to split by."""
    kind = layer.measures[mq.measure].kind
    count_measure = driver_specs().get(mq.measure, {}).get("count")
    counts: dict[str, Any] = {"current": 1, "prior": 1}
    records: list[dict[str, Any]] = []
    ran: list[Ran] = []
    if kind == "sum" and count_measure:
        fetched = await fetch(principal, replace(mq, measure=count_measure, compare_to="prior_period"), layer)
        counted = sides(fetched, ratio=False)
        counts = {period: (counted[period].value or 0) if period in counted else 0 for period in counts}
        records, ran = fetched.records(), [fetched.ran]
    rows = []
    for period, side in (("current", current), ("prior", prior)):
        if kind == "ratio":
            rows.append([period, side.num, side.den])
        else:
            rows.append([period, side.value, side.value if kind == "count" else counts[period]])
    return Split("", Table(["period", "value", "n"], rows), records, ran, {})


def decompose_params(dim: str | None) -> dict[str, Any]:
    return {"value": "value", "count": "n", "period": "period", "base": "prior", "current": "current", "group": dim}


def _share_derivation(now_at: int | None, then_at: int | None, *, ratio: bool, agg: bool, now: Side, then: Side) -> str:
    num, den = ("num", "den") if agg else ("numerator", "denominator") if ratio else ("value", "")
    if ratio:
        cur, pri = f"{num}[{now_at}] / {den}[{now.row}]", f"{num}[{then_at}] / {den}[{then.row}]"
        change = f"({num} / {den})[{now.row}] - ({num} / {den})[{then.row}]"
    else:
        cur, pri = f"{num}[{now_at}]", f"{num}[{then_at}]"
        change = f"{num}[{now.row}] - {num}[{then.row}]"
    # A group missing from one period counts as zero there.
    parts = [cur] if now_at is not None else []
    parts += [f"- {pri}" if parts else f"-{pri}"] if then_at is not None else []
    return f"({' '.join(parts)}) / ({change})"


def contributions(
    result: dict[str, Any], split: Split, offset: int, *, ratio: bool, agg: bool, current: Side, prior: Side
) -> list[Group]:
    """Each group's part of the change. For a sum or count it is the group's own change; for a ratio it is the
    group's numerator over the whole denominator, now minus then, so the parts add up to the ratio's change.

    offset is where split.records start in the evidence rows, so each derivation names the right cells."""
    if current.value is None or prior.value is None or (ratio and not (current.den and prior.den)):
        return []
    change = current.value - prior.value
    groups = []
    for group in result.get("groups") or []:
        key = str(group["group"])
        if ratio:
            assert current.den and prior.den
            part = as_decimal(group["total_current"]) / current.den - as_decimal(group["total_base"]) / prior.den
        else:
            part = as_decimal(group["delta_total"])
        share = part / change if change else Decimal(0)
        now_at, then_at = (split.at.get((period, key)) for period in ("current", "prior"))
        derivation = _share_derivation(
            None if now_at is None else offset + now_at,
            None if then_at is None else offset + then_at,
            ratio=ratio,
            agg=agg,
            now=current,
            then=prior,
        )
        groups.append(Group(split.dim, key, part, share, derivation))
    return groups


def top_groups(groups: Sequence[Group]) -> tuple[Group, ...]:
    pushing = sorted((g for g in groups if g.share > 0), key=lambda g: -g.share)
    if not pushing:
        return ()
    if pushing[0].share >= MAJORITY or len(pushing) == 1:
        return (pushing[0],)
    pair = (pushing[0], pushing[1])
    return pair if pair[0].share + pair[1].share >= MAJORITY else (pushing[0],)


def pick_drivers(per_dimension: Sequence[Sequence[Group]]) -> list[tuple[Group, ...]]:
    """The dimension whose one or two top groups explain the most per group named, and a second dimension's
    single top group when the first named only one and the second also covers most of the change."""
    candidates = [top for groups in per_dimension if len(groups) > 1 and (top := top_groups(groups))]
    candidates.sort(key=lambda top: -sum(g.share for g in top) / len(top))
    if not candidates:
        return []
    chosen = [candidates[0]]
    if len(chosen[0]) == 1:
        second = next((top for top in candidates[1:] if len(top) == 1 and top[0].share >= SECOND_DRIVER), None)
        if second is not None:
            chosen.append(second)
    return chosen
