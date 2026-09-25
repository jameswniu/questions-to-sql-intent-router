import ast
import importlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest

from app.answer.quant import verify_sql
from app.answer.why import DOC_KINDS, LEAD_SQL
from app.config import ROOT
from app.identity import Principal, principal_for
from app.live import analyze, documents, metrics, spec
from app.live.datasets import Dataset
from app.live.errors import Invalid
from app.live.why import answer_why_live, plan
from app.llm.fake import ScriptedLLM, calls, json_reply
from app.sandbox import codecheck, templates
from app.sandbox.client import Table
from app.semantic.compile import compile
from app.semantic.layer import Layer, default_layer
from app.semantic.query import MetricQuery
from tests.live.conftest import (
    BY_PERIL,
    DECOMPOSE_CODE,
    MEMO,
    NOTE,
    PLANTED_HIT,
    QUARANTINED,
    QUESTION,
    YOY_CODE,
    Database,
    NoKey,
    Sandbox,
    Search,
)


@pytest.fixture
def layer() -> Layer:
    return default_layer()


@pytest.fixture
def headline(dana: Principal, layer: Layer) -> MetricQuery:
    planned = plan(dana, QUESTION, None, layer)
    assert planned is not None
    return planned[0]


def by_peril(mq: MetricQuery) -> Dataset:
    grouped = replace(mq, group_by=("peril",))
    rows = tuple(tuple(row) for row in BY_PERIL)
    return Dataset("d2", grouped, "rows", ("period", "peril", "value"), rows, "SELECT ...")


async def test_query_metric_runs_the_checked_sql_as_the_principal_it_was_bound_to(
    dana: Principal, layer: Layer, headline: MetricQuery, database: Database
) -> None:
    queried = await metrics.query_metric(dana, layer, headline, "d1")
    assert queried.status == "ok" and queried.dataset is not None
    [(principal, sql, params)] = database.calls
    compiled = compile(headline, layer, dana)
    assert principal == dana and principal.db_role == "u_adj_west"
    assert sql == verify_sql(compiled.sql, relations=compiled.relations) and params == compiled.params


async def test_an_analyst_reaches_the_aggregates_only(layer: Layer, headline: MetricQuery, database: Database) -> None:
    sam = principal_for("sam")
    await metrics.query_metric(sam, layer, headline, "d1")
    [(principal, sql, _)] = database.calls
    assert principal.db_role == "u_analyst" and "agg.metric" in sql.lower() and "sem.v_payments_net" not in sql


async def test_a_region_the_adjuster_cant_see_is_not_allowed_and_nothing_runs(
    dana: Principal, layer: Layer, headline: MetricQuery, database: Database
) -> None:
    east = replace(headline, filters={"region": ("East",)})
    assert (await metrics.query_metric(dana, layer, east, "d1")).status == "not_allowed"
    assert database.calls == []


async def test_the_orchestrator_cant_name_another_user_in_a_query(
    dana: Principal, layer: Layer, headline: MetricQuery, database: Database, no_key: NoKey
) -> None:
    as_priya = {**spec.to_spec(headline, layer), "user": "priya"}
    llm = ScriptedLLM(calls(("query_metric", as_priya)))
    done = await answer_why_live(llm, dana, QUESTION, layer=layer)
    assert done.fallback == "invalid" and no_key.calls == [QUESTION]
    # Only the planned change ran, before the orchestrator's first turn, and as dana.
    assert [principal for principal, _, _ in database.calls] == [dana]


async def test_find_documents_searches_as_the_principal_and_hands_back_only_validated_picks(
    dana: Principal, layer: Layer, headline: MetricQuery, database: Database, search: Search
) -> None:
    picks = [
        {"chunk_id": MEMO.chunk_id, "driver": "Peril:Hail", "relevance": "explains"},
        {"chunk_id": PLANTED_HIT.chunk_id, "driver": "overall", "relevance": "context"},
        {"chunk_id": "memo-99#made-up:1", "driver": "peril:hail", "relevance": "explains"},
        {"chunk_id": MEMO.chunk_id, "driver": "peril:lava", "relevance": "explains"},
        {"chunk_id": MEMO.chunk_id, "driver": "peril:hail", "relevance": "explains"},
    ]
    llm = ScriptedLLM(json_reply({"picks": picks}))
    found = await documents.find_documents(llm, dana, layer, headline, ["peril:hail"])
    assert [(p, kinds) for p, _, kinds in search.calls] == [(dana, DOC_KINDS)]
    assert [(p, sql) for p, sql, _ in database.calls] == [(dana, LEAD_SQL)]
    [request] = llm.requests
    assert not request.tools and [p.source for p in request.passages] == [MEMO.chunk_id, PLANTED_HIT.chunk_id]
    assert QUARANTINED.body not in json.dumps(request.params()) and NOTE.body not in json.dumps(request.params())
    assert [(c.hit, c.driver, c.relevance) for c in found.chosen] == [
        (MEMO, "peril:hail", "explains"),
        (PLANTED_HIT, "overall", "context"),
    ]
    assert found.status == "ok" and found.dropped == 3


def _imports(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names |= {node.module, *(f"{node.module}.{alias.name}" for alias in node.names)}
    return names


def test_the_analysis_sub_agent_never_imports_the_database() -> None:
    for path in ("app/live/analyze.py", "app/live/datasets.py"):
        assert "app.db" not in _imports(path), path
    probe = "import sys, app.live.analyze; print('app.db' in sys.modules)"
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=dict(os.environ))
    assert done.stdout.strip() == "False", done.stderr[-2000:]


async def test_the_sandbox_gets_the_rows_and_the_model_gets_only_their_columns(
    layer: Layer, headline: MetricQuery, sandbox: Sandbox
) -> None:
    dataset = by_peril(headline)
    llm = ScriptedLLM(json_reply({"code": YOY_CODE}))
    analyzed = await analyze.analyze(llm, "u_adj_west", dataset, "yoy", "a1", layer, sandbox=sandbox)
    assert analyzed.analysis is not None and analyzed.analysis.result["groups"][0]["group"] == "hail"
    table = Table(["period", "peril", "value"], [list(row) for row in BY_PERIL])
    params = {"value": "value", "period": "period", "base": "prior", "current": "current", "group": "peril"}
    assert [(c["role"], c["template"], c["code"] is not None) for c in sandbox.calls] == [
        ("u_adj_west", None, True),
        ("u_adj_west", "yoy", False),
    ]
    assert all(c["table"] == table and c["params"] == params for c in sandbox.calls)
    [request] = llm.requests
    body = json.dumps(request.params())
    assert not request.tools and not request.passages and "21000000" not in body and "peril" in body


async def test_adapted_code_that_disagrees_with_the_golden_template_is_rejected(
    layer: Layer, headline: MetricQuery
) -> None:
    skewed = Sandbox(skew=1.0)
    llm = ScriptedLLM(json_reply({"code": YOY_CODE}))
    with pytest.raises(Invalid, match="disagreed"):
        await analyze.analyze(llm, "u_adj_west", by_peril(headline), "yoy", "a1", layer, sandbox=skewed)


async def test_the_golden_result_is_kept_even_when_the_adapted_one_agrees(layer: Layer, headline: MetricQuery) -> None:
    within_rounding = Sandbox(skew=0.004)
    llm = ScriptedLLM(json_reply({"code": YOY_CODE}))
    dataset = by_peril(headline)
    analyzed = await analyze.analyze(llm, "u_adj_west", dataset, "yoy", "a1", layer, sandbox=within_rounding)
    assert analyzed.analysis is not None
    job = analyze.job(dataset, "yoy", layer)
    assert job is not None
    table, params = job
    golden = templates.yoy(
        pd.DataFrame(json.loads(json.dumps(table.rows, default=float)), columns=table.columns), params
    )
    assert analyzed.analysis.result["change"] == golden["change"]


def _run_time_names(source: str) -> set[str]:
    """The names a module imports and then uses as it runs. Under the future import an annotation is never
    evaluated, so a name that only annotates is left out."""
    tree = ast.parse(source)
    imported = {
        (alias.asname or alias.name).partition(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom) and getattr(node, "module", None) != "__future__"
        for alias in node.names
    }
    annotations = [
        found
        for node in ast.walk(tree)
        for found in (getattr(node, "annotation", None), getattr(node, "returns", None))
        if isinstance(found, ast.expr)
    ]
    annotating = {id(name) for found in annotations for name in ast.walk(found)}
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and id(node) not in annotating} & imported


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """sandbox/runner.py as the sandbox image loads it, with the golden templates beside it."""
    monkeypatch.setitem(sys.modules, "templates", templates)
    monkeypatch.setattr(sys, "path", list(sys.path))
    return importlib.import_module("sandbox.runner")


def test_adapted_code_is_given_every_name_the_golden_templates_run_with(runner: ModuleType) -> None:
    # Code can't import, so a golden template that uses a name the runner doesn't give can't be copied faithfully.
    # decompose's exact split uses Fraction, which the orchestrator runs on every split.
    used = _run_time_names(Path(templates.__file__).read_text())
    assert "Fraction" in used and used <= set(runner.PROVIDED)
    assert tuple(runner.PROVIDED) == codecheck.PROVIDED
    assert f"The sandbox provides {codecheck.PROVIDED_TEXT} as names" in analyze.INSTRUCTIONS
    table = {"columns": ["period", "peril", "value"], "rows": [[p, g, float(v)] for p, g, v in BY_PERIL]}
    params = {"value": "value", "period": "period", "base": "prior", "current": "current", "group": "peril"}
    adapted = runner.execute({"code": DECOMPOSE_CODE, "params": params, "table": table})
    assert adapted == runner.execute({"template": "decompose", "params": params, "table": table})


async def test_the_decompose_template_adapts_as_the_model_writes_it(
    layer: Layer, headline: MetricQuery, sandbox: Sandbox
) -> None:
    dataset = by_peril(headline)
    llm = ScriptedLLM(json_reply({"code": DECOMPOSE_CODE}))
    analyzed = await analyze.analyze(llm, "u_adj_west", dataset, "decompose", "a1", layer, sandbox=sandbox)
    assert analyzed.analysis is not None and analyzed.analysis.result["groups"][0]["group"] == "hail"
    # Before the prompt named Fraction as provided, the model's copy began by importing it, which is refused.
    imported = ScriptedLLM(json_reply({"code": "from fractions import Fraction\n" + DECOMPOSE_CODE}))
    with pytest.raises(Invalid, match="import is not allowed; pd, np, math, statistics and Fraction are provided"):
        await analyze.analyze(imported, "u_adj_west", dataset, "decompose", "a1", layer, sandbox=sandbox)


async def test_adapted_code_the_sandbox_check_refuses_never_runs(
    layer: Layer, headline: MetricQuery, sandbox: Sandbox
) -> None:
    escaping = "import os\n\ndef run(df, params):\n    return {'files': os.listdir('/')}\n"
    llm = ScriptedLLM(json_reply({"code": escaping}))
    with pytest.raises(Invalid, match="refused"):
        await analyze.analyze(llm, "u_adj_west", by_peril(headline), "yoy", "a1", layer, sandbox=sandbox)
    assert sandbox.calls == []
