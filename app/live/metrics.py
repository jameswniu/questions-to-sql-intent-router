from dataclasses import dataclass
from typing import Literal

import psycopg

from app import db
from app.answer.quant import UnsafeSQL, scope, verify_sql
from app.identity import Principal
from app.live.datasets import Dataset
from app.semantic.compile import compile
from app.semantic.layer import Layer
from app.semantic.query import Clarify, MetricQuery, OutOfData
from app.semantic.resolve import resolve

Status = Literal["ok", "clarify", "out_of_data", "not_allowed", "unsafe"]


@dataclass(frozen=True)
class Queried:
    status: Status
    dataset: Dataset | None = None


async def query_metric(principal: Principal, layer: Layer, mq: MetricQuery, handle: str) -> Queried:
    """The SQL sub-agent: the quantitative path's own resolve, scope, compile, check and run, as the principal it
    was bound to. Nothing the model sends can name another user or reach SQL except through the layer."""
    analyst = principal.kind == "analyst"
    asked = layer.measures.get(mq.measure)
    if analyst and asked is not None and not asked.analyst:
        return Queried("not_allowed")
    resolved = resolve(mq, layer, analyst=analyst)
    if isinstance(resolved, Clarify):
        return Queried("clarify")
    if isinstance(resolved, OutOfData):
        return Queried("out_of_data")
    scoped, note = scope(resolved, principal, layer)
    if scoped is None:
        return Queried("not_allowed")
    compiled = compile(scoped, layer, principal)
    try:
        sql = verify_sql(compiled.sql, relations=compiled.relations)
        found = await db.run(principal, sql, compiled.params)
    except UnsafeSQL:
        return Queried("unsafe")
    except (psycopg.errors.InvalidParameterValue, psycopg.errors.RaiseException):
        # The aggregate views refuse splits they can't serve; the quantitative path reads these as not allowed too.
        return Queried("not_allowed")
    rows = tuple(tuple(row) for row in found.rows)
    dataset = Dataset(handle, scoped, compiled.kind, tuple(found.columns), rows, sql, note, tuple(compiled.params))
    return Queried("ok", dataset)
