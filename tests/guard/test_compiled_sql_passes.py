from collections.abc import Iterator

import pytest
import sqlglot
from sqlglot import exp

from app.answer.lookup import SQL as LOOKUP_SQL
from app.answer.qual import LOSS_DATE_SQL
from app.answer.scanfield import PAID_SQL
from app.identity import Principal, principals
from app.semantic.compile import Compiled, compile
from app.semantic.layer import Layer, default_layer
from app.semantic.query import MetricQuery, Period
from app.semantic.resolve import resolve
from app.sqlcheck import ALLOWED_FUNCTIONS, check, function_name

LAYER = default_layer()
BY_KIND = {p.kind: p for p in principals().values()}
Y2025 = Period.year(2025)


def _shapes(layer: Layer, analyst: bool) -> Iterator[MetricQuery]:
    for name, measure in layer.measures.items():
        dims = tuple(d for d in layer.dimensions_for(measure, analyst=analyst) if not layer.dimensions[d].is_grain)
        period = None if measure.point_in_time else Y2025
        yield MetricQuery(name, period=period)
        for dim in dims:
            yield MetricQuery(
                name, filters={dim: layer.dimensions[dim].values[:1]}, group_by=(dim,), period=period, limit=3
            )
        if period is not None:
            yield MetricQuery(name, group_by=dims[:1], period=period, grain="quarter")
            yield MetricQuery(name, group_by=dims[:1], period=period, compare_to="prior_year")
            yield MetricQuery(name, period=period, compare_to="prior_period")


def _compiled(queries: list[MetricQuery], principal: Principal) -> list[Compiled]:
    analyst = principal.kind == "analyst"
    out = []
    for query in queries:
        resolved = resolve(query, LAYER, analyst=analyst)
        if isinstance(resolved, MetricQuery) and (LAYER.measures[resolved.measure].analyst or not analyst):
            out.append(compile(resolved, LAYER, principal))
    return out


def _refused(compiled: list[Compiled]) -> list[tuple[str, str | None]]:
    # The approved SQL is what runs, so it must also keep every placeholder the parameters fill.
    out = []
    for c in compiled:
        verdict = check(c.sql, relations=c.relations)
        if not verdict.ok or verdict.sql.count("%s") != len(c.params):
            out.append((c.sql, verdict.reason))
    return out


@pytest.mark.parametrize("kind", sorted(BY_KIND))
def test_every_layer_example_compiles_to_sql_the_checker_accepts(kind: str) -> None:
    compiled = _compiled([example.query for example in LAYER.examples], BY_KIND[kind])
    assert len(compiled) == len(LAYER.examples)
    assert _refused(compiled) == []


@pytest.mark.parametrize("kind", sorted(BY_KIND))
def test_every_shape_the_compiler_writes_passes_the_checker(kind: str) -> None:
    compiled = _compiled(list(_shapes(LAYER, kind == "analyst")), BY_KIND[kind])
    assert compiled
    assert _refused(compiled) == []


def _emitted() -> tuple[set[str], set[str], set[str]]:
    """The calls, operators and table functions in every statement the app writes, fixed queries included."""
    written = [c.sql for p in BY_KIND.values() for c in _compiled(_everything(p), p)]
    calls: set[str] = set()
    operators: set[str] = set()
    table_functions: set[str] = set()
    for sql in [*written, LOOKUP_SQL, LOSS_DATE_SQL, PAID_SQL]:
        for node in sqlglot.parse_one(sql, read="postgres").find_all(exp.Func, exp.Column):
            name = function_name(node)
            if name is None:
                if isinstance(node, exp.Func):
                    operators.add(type(node).__name__)
            elif isinstance(node.parent, exp.Table) and node.arg_key == "this":
                table_functions.add(name)
            else:
                calls.add(name)
    return calls, operators, table_functions


def test_the_function_allow_list_is_exactly_what_the_app_calls() -> None:
    # The gate: a measure that needs a new function fails here until the name is added to the list on purpose.
    calls, _, _ = _emitted()
    assert calls == ALLOWED_FUNCTIONS


def test_the_app_uses_only_known_operators_and_table_functions() -> None:
    _, operators, table_functions = _emitted()
    assert operators == {"Cast", "Case", "If", "And", "Or"}
    assert table_functions == {"metric"}


def _everything(principal: Principal) -> list[MetricQuery]:
    return [*_shapes(LAYER, principal.kind == "analyst"), *(example.query for example in LAYER.examples)]


def test_the_shapes_include_the_ctes_and_casts_the_checker_inspects() -> None:
    written = " ".join(c.sql for p in BY_KIND.values() for c in _compiled(list(_shapes(LAYER, p.kind == "analyst")), p))
    for fragment in ("WITH num AS", "::numeric", "::date", "::text[]", "::jsonb", "UNION ALL"):
        assert fragment in written
