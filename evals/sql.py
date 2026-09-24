from collections.abc import Sequence
from typing import Any

from app.answer.quant import QuantResult, UnsafeSQL, _cells, verify_sql
from app.db import gold_connection
from app.identity import principal_for
from app.semantic.compile import compile as compile_query
from app.semantic.layer import default_layer
from evals.outcome import Outcome, Verdict, rate
from evals.splits import Case
from tests.quant.test_dev_accuracy import gold_rows, misses


def streamed_result(o: Outcome) -> QuantResult | str:
    """The pipeline's answer as the QuantResult the tests compare, rebuilt from the rows it streamed and the query
    it remembered, or why it can't be. The rebuilt query must compile to the SQL that ran, so the rows are read
    under the grouping they were fetched with."""
    if o.route != "quantitative" or o.outcome != "answer":
        return f"answered as {o.route}, {o.outcome}"
    principal = principal_for(o.user)
    turn = o.logged.memory.get(principal.user_id, o.logged.session_id)
    ran = [payload["sql"] for payload in o.evidence("sql")]
    rows: list[dict[str, Any]] = next(iter(o.evidence("rows")), [])
    if turn is None or turn.query is None or len(ran) != 1:
        return "the answer left no single query to check"
    query, layer = turn.query, default_layer()
    try:
        compiled = compile_query(query, layer, principal)
        same = verify_sql(compiled.sql, relations=compiled.relations) == ran[0]
    except (UnsafeSQL, ValueError, KeyError) as exc:
        return f"the remembered query does not compile: {exc}"
    if not same or (rows and set(rows[0]) != set(compiled.columns)):
        return "the remembered query is not the one that ran"
    records = [tuple(row[column] for column in compiled.columns) for row in rows]
    dims = [*query.group_by, *([query.grain] if query.grain else [])]
    cells = _cells(compiled, records, dims, layer.measures[query.measure].kind == "ratio")
    text = o.answer.text if o.answer else ""
    return QuantResult("answer", text, layer.as_of, query=query, cells=tuple(cells))


def shape_problem(case: Case, result: QuantResult) -> str | None:
    """The comparison reads a groups case's first key, so a single figure given where groups were asked for is
    a miss to record here, before it reaches the comparison."""
    ungrouped = any(not cell.keys for cell in result.cells if cell.period == "current")
    return "gave one figure where the case asks for groups" if case["match"] == "groups" and ungrouped else None


def execution_misses(outcomes: Sequence[Outcome]) -> dict[str, list[str]]:
    """Each case's differences from its gold SQL, run as gold_reader over the asker's regions, under the
    comparison tests/quant uses."""
    found: dict[str, list[str]] = {}
    with gold_connection() as conn:
        for o in outcomes:
            result = streamed_result(o)
            if isinstance(result, str):
                found[o.case["id"]] = [result]
            elif (problem := shape_problem(o.case, result)) is not None:
                found[o.case["id"]] = [problem]
            else:
                found[o.case["id"]] = misses(o.case, result, gold_rows(conn, o.case))
    return found


def score_sql(outcomes: Sequence[Outcome]) -> tuple[dict[str, Any], list[Verdict]]:
    found = execution_misses(outcomes)
    right = {case_id: not problems for case_id, problems in found.items()}
    by_kind: dict[str, list[bool]] = {}
    for o in outcomes:
        by_kind.setdefault(principal_for(o.user).kind, []).append(right[o.case["id"]])
    section = {
        "execution_accuracy": rate(sum(right.values()), len(right)),
        "by_kind": {kind: rate(sum(marks), len(marks)) for kind, marks in sorted(by_kind.items())},
        "misses": {case_id: "; ".join(problems) for case_id, problems in sorted(found.items()) if problems},
    }
    verdicts = [Verdict(o.answered, right[o.case["id"]]) for o in outcomes]
    return section, verdicts
