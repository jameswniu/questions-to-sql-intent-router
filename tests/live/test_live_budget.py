import asyncio
import time
from dataclasses import dataclass
from typing import Any, NoReturn

import pytest

from app import db, pipeline, telemetry
from app.identity import Principal, principal_for
from app.live import extract, residue, spec, support, writer
from app.live import why as live_why
from app.live.errors import Fallback
from app.live.extract import extract_live
from app.live.residue import route_residue
from app.live.why import answer_why_live, plan
from app.llm.client import Response
from app.llm.fake import ScriptedLLM, calls, cited, json_reply, reply
from app.llm.request import Request
from app.semantic.layer import Layer, default_layer
from tests.live.conftest import (
    HEADLINE,
    MEMO,
    MEMO_SENTENCE,
    QUESTION,
    YOY_CODE,
    Database,
    NoKey,
    Sandbox,
    Search,
    ticking,
)

PICKS = json_reply({"picks": [{"chunk_id": MEMO.chunk_id, "driver": "peril:hail", "relevance": "explains"}]})


async def hang(*_: object, **__: object) -> NoReturn:
    await asyncio.sleep(30)
    raise AssertionError("a deadline should have stopped this call")


@dataclass
class Stalling(Database):
    """The fake database, hanging from its second query on."""

    async def run(self, principal: Principal, query: Any, params: Any = None, **options: Any) -> db.Rows:
        if self.calls:
            self.calls.append((principal, str(query), params))
            await hang()
        return await super().run(principal, query, params, **options)


@dataclass
class CurrentOnly(Database):
    """The fake database, answering the planned change with this period's figure and no figure before it."""

    async def run(self, principal: Principal, query: Any, params: Any = None, **options: Any) -> db.Rows:
        found = await super().run(principal, query, params, **options)
        if found.columns != ["period", "value"]:
            return found
        return db.Rows(found.columns, [row for row in found.rows if row[0] == "current"], False, 1.0, found.sql)


def documented(dana: Principal, layer: Layer, *after: Any) -> ScriptedLLM:
    """An orchestrator that splits the change by peril, finds the hail memo and finishes on both, then whatever comes
    after: the writer and the reading."""
    return ScriptedLLM(
        calls(("query_metric", {**headline(dana, layer), "group_by": ["peril"]})),
        calls(("analyze", {"template": "yoy", "dataset": "d2"}), ("find_documents", {"drivers": ["peril:hail"]})),
        json_reply({"code": YOY_CODE}),
        PICKS,
        calls(("finish", {"analyses": ["a1"], "documents": ["c1"]})),
        *after,
    )


@pytest.fixture
def layer() -> Layer:
    return default_layer()


def headline(dana: Principal, layer: Layer) -> dict[str, Any]:
    planned = plan(dana, QUESTION, None, layer)
    assert planned is not None
    return spec.to_spec(planned[0], layer)


async def test_the_ninth_step_never_happens(dana: Principal, layer: Layer, database: Database, no_key: NoKey) -> None:
    llm = ScriptedLLM(*(calls(("query_metric", headline(dana, layer))) for _ in range(9)))
    done = await answer_why_live(llm, dana, QUESTION, layer=layer)
    assert len(llm.sent(live_why.TEMPLATE)) == live_why.MAX_STEPS == 8 and llm.left == 1
    assert (done.live, done.fallback, done.steps) == (False, "steps", 8)
    assert no_key.calls == [QUESTION] and done.result.text == "no-key answer"


async def test_a_run_past_its_wall_clock_falls_back_to_the_no_key_workflow(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    llm = ScriptedLLM(*(calls(("query_metric", headline(dana, layer))) for _ in range(8)))
    # Each reading of the clock is ten seconds on: the run starts at 0, its first step reads 10 and that step's
    # tool 20, and the second step reads 30, past the 25 s budget.
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, clock=ticking(10.0))
    assert live_why.BUDGET_S == 25.0
    assert (done.live, done.fallback, done.steps) == (False, "time", 1)
    assert len(llm.sent(live_why.TEMPLATE)) == 1 and no_key.calls == [QUESTION]


async def test_a_fallback_is_recorded_on_the_request_span(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    llm = ScriptedLLM(*(calls(("query_metric", headline(dana, layer))) for _ in range(8)))
    with telemetry.tracer().start_as_current_span("claims_qa.ask") as span:
        await answer_why_live(llm, dana, QUESTION, layer=layer, clock=ticking(10.0))
    events = [(e.name, dict(e.attributes or {})) for e in span.events]  # type: ignore[attr-defined]
    assert ("claims_qa.live_fallback", {"reason": "time", "steps": 1}) in events


async def test_a_question_the_no_key_workflow_answers_on_its_own_never_reaches_a_model(
    dana: Principal, layer: Layer, no_key: NoKey
) -> None:
    llm = ScriptedLLM()
    done = await answer_why_live(llm, dana, "Why did paid losses go up?", layer=layer)
    assert (done.live, done.fallback, done.steps) == (False, None, 0)
    assert llm.requests == [] and no_key.calls == ["Why did paid losses go up?"]


async def test_a_model_call_that_hangs_is_cut_off_at_the_budget(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    async def hang(request: Request) -> Response:
        await asyncio.sleep(30)
        raise AssertionError("the budget should have stopped this call")

    started = time.monotonic()
    done = await answer_why_live(ScriptedLLM(hang), dana, QUESTION, layer=layer, budget_s=0.3)
    assert (done.live, done.fallback) == (False, "time") and time.monotonic() - started < 5
    assert no_key.calls == [QUESTION]


async def test_a_turn_asking_for_more_calls_than_the_cap_runs_none_of_them(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    many = calls(*(("query_metric", headline(dana, layer)) for _ in range(live_why.MAX_CALLS_PER_TURN + 1)))
    done = await answer_why_live(ScriptedLLM(many), dana, QUESTION, layer=layer)
    assert (done.live, done.fallback) == (False, "calls")
    assert len(database.calls) == 1, "only the planned change ran"


async def test_a_run_can_make_only_so_many_tool_calls(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    full = calls(*(("query_metric", headline(dana, layer)) for _ in range(live_why.MAX_CALLS_PER_TURN)))
    turns = live_why.MAX_CALLS // live_why.MAX_CALLS_PER_TURN
    llm = ScriptedLLM(*(full for _ in range(turns)), calls(("query_metric", headline(dana, layer))))
    done = await answer_why_live(llm, dana, QUESTION, layer=layer)
    assert (done.live, done.fallback, done.steps) == (False, "calls", turns + 1)
    assert len(database.calls) == 1 + live_why.MAX_CALLS


async def test_a_seed_query_that_hangs_is_cut_off_at_the_budget(
    dana: Principal, layer: Layer, monkeypatch: pytest.MonkeyPatch, no_key: NoKey
) -> None:
    monkeypatch.setattr(db, "run", hang)
    llm = ScriptedLLM()
    started = time.monotonic()
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, budget_s=0.3)
    assert (done.live, done.fallback) == (False, "time") and time.monotonic() - started < 5
    assert llm.requests == [] and no_key.calls == [QUESTION]


async def test_a_tool_call_that_hangs_is_cut_off_at_the_budget(
    dana: Principal, layer: Layer, monkeypatch: pytest.MonkeyPatch, no_key: NoKey
) -> None:
    stalling = Stalling()
    monkeypatch.setattr(db, "run", stalling.run)
    started = time.monotonic()
    llm = ScriptedLLM(calls(("query_metric", headline(dana, layer))))
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, budget_s=0.3)
    assert (done.live, done.fallback, done.steps) == (False, "time", 1) and time.monotonic() - started < 5
    assert len(stalling.calls) == 2, "the seed ran and the orchestrator's query hung"


async def test_a_writer_that_hangs_is_cut_off_at_the_budget(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    llm = documented(dana, layer, hang)
    started = time.monotonic()
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, budget_s=1.0, sandbox=sandbox)
    assert (done.live, done.fallback) == (False, "time") and time.monotonic() - started < 5
    assert llm.requests[-1].template == writer.WHY_TEMPLATE


async def test_a_reading_that_hangs_is_cut_off_at_the_budget(
    dana: Principal, layer: Layer, database: Database, search: Search, sandbox: Sandbox, no_key: NoKey
) -> None:
    llm = documented(dana, layer, reply(cited(MEMO_SENTENCE, (MEMO.chunk_id, 0))), hang)
    started = time.monotonic()
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, budget_s=1.0, sandbox=sandbox)
    assert (done.live, done.fallback) == (False, "reading") and time.monotonic() - started < 5
    assert llm.requests[-1].template == support.TEMPLATE


async def test_residue_routing_and_extraction_stop_at_their_deadlines(
    layer: Layer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(residue, "DEADLINE_S", 0.2)
    monkeypatch.setattr(extract, "DEADLINE_S", 0.2)
    started = time.monotonic()
    assert await route_residue(ScriptedLLM(hang), "banana") == Fallback("time")
    asked = "what did we shell out"
    assert await extract_live(ScriptedLLM(hang), principal_for("priya"), asked, layer) == Fallback("time")
    assert time.monotonic() - started < 5


def test_the_deadlines_are_the_ones_the_route_budgets_are_sized_for() -> None:
    # The pipeline's route timeouts have to leave room for these, so a change here is a change there too.
    assert (residue.DEADLINE_S, extract.DEADLINE_S, writer.QUAL_DEADLINE_S, support.DEADLINE_S) == (5.0, 5.0, 20.0, 8.0)
    assert (live_why.BUDGET_S, live_why.STEP_TIMEOUT_S, live_why.MAX_STEPS) == (25.0, 20.0, 8)
    assert support.DEADLINE_S < writer.QUAL_DEADLINE_S and live_why.STEP_TIMEOUT_S < live_why.BUDGET_S


async def test_a_change_with_nothing_to_compare_it_with_never_reaches_a_model(
    dana: Principal, layer: Layer, monkeypatch: pytest.MonkeyPatch, no_key: NoKey
) -> None:
    monkeypatch.setattr(db, "run", CurrentOnly().run)
    llm = ScriptedLLM()
    done = await answer_why_live(llm, dana, QUESTION, layer=layer)
    # The no-key workflow says there isn't enough data to compare, so this isn't a fallback.
    assert (done.live, done.fallback, done.steps) == (False, None, 0)
    assert llm.requests == [] and no_key.calls == [QUESTION] and HEADLINE[1][0] == "prior"


@pytest.mark.parametrize(
    ("route", "deadline"),
    [
        ("why", live_why.BUDGET_S),
        ("qualitative", writer.QUAL_DEADLINE_S),
        ("quantitative", extract.DEADLINE_S),
        ("clarify", extract.DEADLINE_S),
    ],
)
def test_a_live_route_leaves_room_for_its_deadline_and_then_the_no_key_answer(route: str, deadline: float) -> None:
    assert pipeline.timeout_for(route, live=True) >= pipeline.timeout_for(route) + deadline
