import json
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import TupleRow

from app.answer.quant import QuantResult, answer_quant
from app.config import ROOT
from app.identity import principal_for
from app.semantic.layer import Layer

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]
ALL_REGIONS = ["North", "South", "East", "West"]
CASES = [
    case
    for case in map(json.loads, (ROOT / "evals" / "cases" / "quantitative.jsonl").read_text().splitlines())
    if case["split"] == "dev"
]


def close(mine: Decimal | None, gold: Any) -> bool:
    if mine is None or gold is None:
        return mine is None and gold is None
    expected = Decimal(gold)
    return abs(mine - expected) <= max(abs(expected) * Decimal("0.005"), Decimal("0.01"))


def key(value: Any, grain: str | None) -> str:
    if isinstance(value, date):
        if grain == "quarter":
            return f"{value.year}-Q{(value.month - 1) // 3 + 1}"
        return f"{value:%Y-%m}" if grain == "month" else str(value.year)
    return str(value)


def gold_rows(conn: Connection, case: dict[str, Any]) -> list[dict[str, Any]]:
    principal = principal_for(case["user"])
    regions = list(principal.regions) if principal.kind == "adjuster" else ALL_REGIONS
    cur = conn.execute(case["gold_sql"], {"regions": regions})
    names = [column.name for column in cur.description or ()]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def misses(case: dict[str, Any], result: QuantResult, gold: list[dict[str, Any]]) -> list[str]:
    if result.kind != "answer":
        return [f"answered {result.kind}: {result.text}"]
    current = {cell.keys: cell.value for cell in result.cells if cell.period == "current"}
    if case["match"] == "scalar":
        mine = next(iter(current.values()), None)
        return [] if close(mine, gold[0]["value"]) else [f"value {mine} != gold {gold[0]['value']}"]
    if case["match"] == "compare":
        prior = {cell.keys: cell.value for cell in result.cells if cell.period == "prior"}
        pairs = [
            ("current", current.get(()), gold[0]["value_current"]),
            ("prior", prior.get(()), gold[0]["value_prior"]),
        ]
        return [f"{label} {mine} != gold {want}" for label, mine, want in pairs if not close(mine, want)]
    assert result.query is not None
    grain = result.query.grain
    mine_groups = {key(keys[0], grain): value for keys, value in current.items()}
    gold_groups = {key(row["grp"], grain): row["value"] for row in gold}
    if set(mine_groups) != set(gold_groups):
        return [f"groups {sorted(mine_groups)} != gold {sorted(gold_groups)}"]
    return [f"{k}: {mine_groups[k]} != gold {v}" for k, v in gold_groups.items() if not close(mine_groups[k], v)]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_dev_case_matches_its_gold_answer(
    layer: Layer, login: Callable[..., Connection], case: dict[str, Any]
) -> None:
    gold = gold_rows(login("gold_reader"), case)
    result = await answer_quant(principal_for(case["user"]), case["q"], None, layer=layer)
    assert misses(case, result, gold) == []
