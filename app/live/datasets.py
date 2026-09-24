from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.llm.facts import Fact, IsolationViolation, Token
from app.semantic.layer import Layer
from app.semantic.query import GRAINS, MetricQuery

PERIODS = ("current", "prior")
NUMBER_COLUMNS = frozenset({"value", "numerator", "denominator", "num", "den", "n"})


@dataclass(frozen=True)
class Dataset:
    """A query's result as query_metric ran it, kept in the process under the handle the orchestrator knows it by."""

    handle: str
    query: MetricQuery
    kind: Literal["rows", "agg"]
    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    sql: str
    note: str | None = None
    # The values bound to the statement's placeholders, in order, which the evidence panel shows beside it.
    params: tuple[Any, ...] = ()

    def records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, row, strict=True)) for row in self.rows]


def _word(dim: str, value: Any, layer: Layer) -> Fact:
    if dim not in layer.dimensions:
        raise IsolationViolation(f"{dim!r} isn't a dimension of the layer")
    if value is None:
        return None
    if layer.dimensions[dim].is_grain:
        return Token.day(value) if isinstance(value, date) else Token(str(value))
    return Token.word(str(value), layer.dimensions[dim].values)


def _number(value: Any) -> Fact:
    if value is None or (isinstance(value, int | float | Decimal) and not isinstance(value, bool)):
        return value
    raise IsolationViolation(f"{value!r} isn't a number")


def row_facts(record: Mapping[str, Any], layer: Layer) -> dict[str, Fact]:
    """One result row as a tool result may carry it: its numbers, and its groups as the layer's own values."""
    out: dict[str, Fact] = {}
    for column, cell in record.items():
        if column == "period":
            out[column] = Token.word(str(cell), PERIODS)
        elif column == "grp":
            for dim, value in (cell or {}).items():
                out[dim] = _word(dim, value, layer)
        elif column == "suppressed":
            out[column] = bool(cell)
        elif column in NUMBER_COLUMNS:
            out[column] = _number(cell)
        elif column in GRAINS or column in layer.dimensions:
            out[column] = _word(column, cell, layer)
        else:
            raise IsolationViolation(f"a result column {column!r} can't go into a tool result")
    return out


def summary(dataset: Dataset, layer: Layer) -> dict[str, Fact]:
    """What the orchestrator learns from query_metric: the handle, the measure, and every row's numbers and groups."""
    return {
        "status": Token("ok"),
        "dataset": Token.handle(dataset.handle),
        "measure": Token.word(dataset.query.measure, tuple(layer.measures)),
        "rows": [row_facts(record, layer) for record in dataset.records()],
    }
