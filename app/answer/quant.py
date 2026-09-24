from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, Literal

import psycopg

from app import db, sqlcheck
from app.answer.format import Format, NumberRef, change_refs, long_date, ref
from app.identity import Principal
from app.semantic.compile import Compiled, compile
from app.semantic.describe import display_name, group_label, is_plural, join, places, row_label, state_regions, subject
from app.semantic.extract import extract
from app.semantic.layer import Layer
from app.semantic.query import Clarify, MetricQuery, OutOfData
from app.semantic.resolve import resolve

Kind = Literal["answer", "clarify", "out_of_data", "not_allowed", "error"]
HIDDEN_LINE = "Groups marked withheld cover too few claims, or one claim dominates them, so they aren't shown."


@dataclass(frozen=True)
class Cell:
    keys: tuple[Any, ...]
    period: str
    value: Decimal | None
    hidden: bool
    row_index: int
    column: str = "value"
    derivation: str = "value"


@dataclass(frozen=True)
class QuantResult:
    kind: Kind
    text: str
    data_as_of: date
    numbers: tuple[NumberRef, ...] = ()
    query: MetricQuery | None = None
    clarify: Clarify | None = None
    sql: str | None = None
    params: tuple[Any, ...] = ()
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[Any, ...], ...] = ()
    cells: tuple[Cell, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)


class UnsafeSQL(Exception):
    pass


def verify_sql(sql: str, *, relations: frozenset[str]) -> str:
    """Passes the statement through the shared checker and returns the normalized SQL it approved."""
    verdict = sqlcheck.check(sql, relations=relations)
    if not verdict.ok:
        raise UnsafeSQL(verdict.reason)
    return verdict.sql


def _cells(compiled: Compiled, rows: list[tuple[Any, ...]], dims: list[str], ratio: bool) -> list[Cell]:
    cols = compiled.columns
    cells = []
    for index, row in enumerate(rows):
        record = dict(zip(cols, row, strict=True))
        period = str(record.get("period", "current"))
        if compiled.kind == "agg":
            grp = record["grp"] or {}
            num, den = record["num"], record["den"]
            value = (num / den if den else None) if ratio else num
            keys = tuple(grp.get(dim) for dim in dims)
            derivation = "num / den" if ratio else "num"
            cells.append(Cell(keys, period, value, bool(record["suppressed"]), index, "num", derivation))
        else:
            derivation = "numerator / denominator" if ratio else "value"
            keys = tuple(record[dim] for dim in dims)
            cells.append(Cell(keys, period, record["value"], False, index, "value", derivation))
    return cells


def _scope(mq: MetricQuery, principal: Principal, layer: Layer) -> tuple[MetricQuery | None, str | None]:
    """Keeps an adjuster's question inside their regions and says what that leaves out."""
    visible = set(principal.regions)
    if principal.kind != "adjuster" or visible >= set(layer.dimensions["region"].values):
        return mq, None
    seen = join(f"the {r}" for r in sorted(visible))
    asked_states = mq.filters.get("state", ())
    asked = set(mq.filters.get("region", ())) | {state_regions()[s] for s in asked_states}
    if not asked:
        note = f"You can see claims in {seen} only." if "region" in mq.group_by else f"This covers {seen}."
        return mq, note
    hidden = asked - visible
    if not hidden:
        return mq, None
    if hidden == asked:
        return None, f"You can see claims in {seen} only, so I can't report on {places(mq.filters)}."
    filters = dict(mq.filters)
    filters["region"] = tuple(r for r in filters.get("region", ()) if r in visible)
    filters["state"] = tuple(s for s in asked_states if state_regions()[s] in visible)
    kept = {dim: values for dim, values in filters.items() if values}
    left_out = join(f"the {r}" for r in sorted(hidden))
    return replace(mq, filters=kept), f"You can see claims in {seen} only, so this leaves out {left_out}."


def _movement(now: Decimal, then: Decimal, changes: tuple[NumberRef, ...]) -> str:
    if now == then:
        return "unchanged"
    pct = f" ({changes[1].display})" if len(changes) > 1 else ""
    return f"{'up' if now > then else 'down'} {changes[0].display}{pct}"


def _ref(cell: Cell, fmt: Format) -> NumberRef:
    assert cell.value is not None
    return ref(cell.value, fmt, cell.column, cell.row_index, cell.derivation)


def _verb(plural: bool) -> str:
    return "were" if plural else "was"


def _scalar(mq: MetricQuery, layer: Layer, cells: list[Cell]) -> tuple[str, list[NumberRef]]:
    measure = layer.measures[mq.measure]
    what = subject(mq, layer)
    when = f"as of {long_date(layer.as_of)}" if measure.point_in_time else f"in {mq.period.label if mq.period else ''}"
    current = next((c for c in cells if c.period == "current"), None)
    if current is None or current.hidden or current.value is None:
        if current is not None and current.hidden:
            return f"{what} {when} is withheld: it covers too few claims, or one claim dominates it.", []
        return f"There is no data for {subject(mq, layer, capital=False)} {when}.", []
    now = _ref(current, measure.format)
    prior = next((c for c in cells if c.period == "prior"), None)
    if mq.compare_to is None or prior is None or mq.period is None:
        return f"{what} {_verb(is_plural(measure))} {now.display} {when}.", [now]
    before = mq.period.years_earlier() if mq.compare_to == "prior_year" else mq.period.prior()
    if prior.hidden or prior.value is None:
        return f"{what} {_verb(is_plural(measure))} {now.display} {when}; {before.label} is withheld.", [now]
    then = _ref(prior, measure.format)
    head = f"{what} {_verb(is_plural(measure))} {now.display} {when} and {then.display} in {before.label}"
    changes = change_refs(
        current.value, prior.value, measure.format, (current.row_index, prior.row_index), current.derivation
    )
    return f"{head}, {_movement(current.value, prior.value, changes)}.", [now, then, *changes]


def _groups(mq: MetricQuery, layer: Layer, cells: list[Cell], dims: list[str]) -> tuple[str, list[NumberRef]]:
    measure = layer.measures[mq.measure]
    fmt = measure.format
    when = f"as of {long_date(layer.as_of)}" if measure.point_in_time else f"in {mq.period.label if mq.period else ''}"
    by = join(dims)
    current = [c for c in cells if c.period == "current"]
    prior = {c.keys: c for c in cells if c.period == "prior"}
    shown = [c for c in current if not c.hidden and c.value is not None]
    refs: list[NumberRef] = []
    lines = []
    for cell in current:
        label = row_label(dims, cell.keys)
        if cell.hidden:
            lines.append(f"- {label}: withheld")
            continue
        if cell.value is None:
            lines.append(f"- {label}: no data")
            continue
        figure = _ref(cell, fmt)
        refs.append(figure)
        line = f"- {label}: {figure.display}"
        before = prior.get(cell.keys)
        if before is not None and before.value is not None and not before.hidden:
            then = _ref(before, fmt)
            changes = change_refs(cell.value, before.value, fmt, (cell.row_index, before.row_index), cell.derivation)
            refs += [then, *changes]
            line += f" against {then.display}, {_movement(cell.value, before.value, changes)}"
        lines.append(line)
    if not shown:
        summary = f"There is no data to show for {subject(mq, layer, capital=False)} by {by} {when}."
    elif len(shown) == 1:
        only = shown[0]
        figure = next(r for r in refs if r.row_index == only.row_index)
        verb = _verb(is_plural(measure))
        summary = f"{subject(mq, layer)} {when} {verb} {figure.display} for {row_label(dims, only.keys)}."
    else:
        high = max(shown, key=lambda c: (c.value, -c.row_index))
        low = min(shown, key=lambda c: (c.value, c.row_index))
        hi = next(r for r in refs if r.row_index == high.row_index)
        lo = next(r for r in refs if r.row_index == low.row_index)
        if mq.grain is not None and dims == [mq.grain]:
            summary = (
                f"{subject(mq, layer)} by {mq.grain} {when} ranged from {lo.display} in "
                f"{group_label(mq.grain, low.keys[0])} to {hi.display} in {group_label(mq.grain, high.keys[0])}."
            )
        else:
            summary = (
                f"{subject(mq, layer)} {when} {_verb(is_plural(measure))} highest for {row_label(dims, high.keys)} "
                f"at {hi.display} "
                f"and lowest for {row_label(dims, low.keys)} at {lo.display}."
            )
    text = "\n".join([summary, *lines])
    if any(c.hidden for c in cells):
        text += f"\n{HIDDEN_LINE}"
    return text, refs


def _shape(mq: MetricQuery, layer: Layer, cells: list[Cell]) -> tuple[str, list[NumberRef]]:
    dims = [*mq.group_by, *([mq.grain] if mq.grain else [])]
    return _groups(mq, layer, cells, dims) if dims else _scalar(mq, layer, cells)


def _result(kind: Kind, text: str, layer: Layer, **extra: Any) -> QuantResult:
    return QuantResult(kind=kind, text=text, data_as_of=layer.as_of, **extra)


async def answer_quant(
    principal: Principal, question: str, previous: MetricQuery | None, *, layer: Layer
) -> QuantResult:
    extracted = extract(question, layer, previous)
    if isinstance(extracted, Clarify):
        return _result("clarify", extracted.question, layer, clarify=extracted, query=extracted.partial)
    analyst = principal.kind == "analyst"
    asked = layer.measures.get(extracted.measure)
    if analyst and asked is not None and not asked.analyst:
        text = f"Analysts see aggregates only, and {display_name(asked)} aren't kept as an aggregate."
        return _result("not_allowed", text, layer, query=extracted)
    resolved = resolve(extracted, layer, analyst=analyst)
    if isinstance(resolved, Clarify):
        return _result("clarify", resolved.question, layer, clarify=resolved, query=resolved.partial)
    if isinstance(resolved, OutOfData):
        return _result("out_of_data", resolved.reason, layer, query=extracted)
    scoped, scope_note = _scope(resolved, principal, layer)
    if scoped is None:
        return _result("not_allowed", scope_note or "", layer, query=resolved)
    compiled = compile(scoped, layer, principal)
    try:
        sql = verify_sql(compiled.sql, relations=compiled.relations)
        found = await db.run(principal, sql, compiled.params)
    except UnsafeSQL:
        return _result("error", "I couldn't build a safe query for that question.", layer, query=scoped)
    except db.QueryTimeout:
        return _result("error", "That query ran too long, so I stopped it.", layer, query=scoped)
    except (psycopg.errors.InvalidParameterValue, psycopg.errors.RaiseException):
        text = "The aggregate views can't answer that as asked. Try whole months, or a month, quarter or year split."
        return _result("not_allowed", text, layer, query=scoped)
    dims = [*scoped.group_by, *([scoped.grain] if scoped.grain else [])]
    cells = _cells(compiled, found.rows, dims, layer.measures[scoped.measure].kind == "ratio")
    text, numbers = _shape(scoped, layer, cells)
    notes = [*scoped.notes, *([scope_note] if scope_note else [])]
    separator = "\n" if "\n" in text else " "
    return _result(
        "answer",
        separator.join([text, *notes]),
        layer,
        numbers=tuple(numbers),
        query=scoped,
        sql=sql,
        params=compiled.params,
        columns=tuple(found.columns),
        rows=tuple(found.rows),
        cells=tuple(cells),
        provenance=compiled.provenance,
    )
