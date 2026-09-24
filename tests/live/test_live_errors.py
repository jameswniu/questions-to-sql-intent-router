from datetime import date
from typing import Any, get_args

import pytest

from app.answer.why import NO_DOCUMENT
from app.handle import HANDLERS
from app.identity import Principal, principal_for
from app.live import extract, residue, spec
from app.live.errors import Fallback
from app.live.extract import extract_live, wanted
from app.live.residue import route_residue
from app.live.why import answer_why_live, plan
from app.llm.client import Refused, Response
from app.llm.fake import ScriptedLLM, calls, json_reply, reply
from app.llm.request import Message
from app.semantic.layer import Layer, default_layer
from app.semantic.query import Clarify, MetricQuery, Period
from tests.live.conftest import QUESTION, YOY_CODE, Database, NoKey, Sandbox

REFUSED = reply("", stop_reason="refusal")
CUT_OFF = reply('{"route": "qu', stop_reason="max_tokens")
FREE_TEXT = reply("I think this one is about claims.")


@pytest.fixture
def layer() -> Layer:
    return default_layer()


async def test_the_fast_model_routes_residue_from_the_question_alone() -> None:
    llm = ScriptedLLM(json_reply({"route": "Why", "reason": "asks what drove a figure"}))
    routing = await route_residue(llm, "what's behind the jump in hail payouts out west")
    assert isinstance(routing, residue.Routing)
    assert routing.routed.route == "why" and routing.routed.source == "model"
    [request] = llm.requests
    assert request.model == llm.fast_model and not request.tools and not request.passages
    assert request.messages == (Message("user", ("what's behind the jump in hail payouts out west",)),)
    assert request.template == residue.TEMPLATE


def test_the_residue_labels_are_the_routers_own() -> None:
    assert set(get_args(residue.Label)) == set(HANDLERS)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (FREE_TEXT, "invalid_output"),
        (json_reply({"route": "sql", "reason": "x"}), "invalid_output"),
        (REFUSED, "refusal"),
        (CUT_OFF, "max_tokens"),
        (Refused("declined"), "refusal"),
    ],
)
async def test_residue_routing_falls_back_to_the_rules_on_any_failure(
    failure: Response | Exception, reason: str
) -> None:
    assert await route_residue(ScriptedLLM(failure), "banana") == Fallback(reason)


def spec_for(layer: Layer, **fields: Any) -> dict[str, Any]:
    base = spec.to_spec(MetricQuery("paid_losses"), layer)
    return {**base, **fields}


async def test_the_fast_model_extracts_a_query_the_rules_couldnt_read(layer: Layer) -> None:
    written = spec_for(
        layer, period="2025-Q2", filters={**spec_for(layer)["filters"], "peril": ["Hail"], "state": ["co"]}
    )
    llm = ScriptedLLM(json_reply(written))
    question = "what did we shell out for hail in Colorado last spring"
    mq = await extract_live(llm, principal_for("priya"), question, layer)
    assert mq == MetricQuery("paid_losses", {"peril": ("hail",), "state": ("CO",)}, period=Period.quarter(2025, 2))
    [request] = llm.requests
    assert request.model == llm.fast_model and not request.tools and not request.passages
    assert request.output is not None and request.messages == (Message("user", (question,)),)


async def test_an_extracted_query_without_a_period_still_goes_on_so_the_user_is_asked_for_one(layer: Layer) -> None:
    llm = ScriptedLLM(json_reply(spec_for(layer)))
    mq = await extract_live(llm, principal_for("priya"), "what did we shell out", layer)
    assert mq == MetricQuery("paid_losses")


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (FREE_TEXT, "invalid_output"),
        (REFUSED, "refusal"),
        (CUT_OFF, "max_tokens"),
        ("unknown measure", "invalid"),
        ("unknown value", "invalid"),
        ("extra field", "invalid"),
    ],
)
async def test_extraction_that_doesnt_hold_keeps_the_rules_clarifying_question(
    layer: Layer, failure: object, reason: str
) -> None:
    replies = {
        "unknown measure": json_reply(spec_for(layer, measure="revenue", period="2025")),
        "unknown value": json_reply(spec_for(layer, period="2025", filters={"peril": ["lava"]})),
        "extra field": json_reply({**spec_for(layer, period="2025"), "sql": "SELECT * FROM core.claims"}),
    }
    step = replies[failure] if isinstance(failure, str) else failure
    assert isinstance(step, Response)
    assert await extract_live(ScriptedLLM(step), principal_for("priya"), "q", layer) == Fallback(reason)


def test_the_few_shot_examples_are_the_layers_own_and_read_back_the_same(layer: Layer) -> None:
    system = extract.request("claude-haiku-4-5", "q", layer).system[0]
    for example in layer.examples:
        assert example.q in system
        assert spec.parse(spec.to_spec(example.query, layer), layer) == example.query


def test_only_a_question_the_rules_couldnt_read_goes_to_the_model() -> None:
    assert wanted(Clarify("measure", "Which figure do you want?", ()))
    assert not wanted(Clarify("period", "For which period?", ()))


def test_a_period_is_one_of_the_compact_forms_or_a_range(layer: Layer) -> None:
    assert spec.parse_period("2025-01-01..2025-02-15") == Period.between(date(2025, 1, 1), date(2025, 2, 15))
    assert spec.period_text(Period.between(date(2025, 1, 1), date(2025, 2, 15))) == "2025-01-01..2025-02-15"
    with pytest.raises(Exception, match="period"):
        spec.parse_period("last spring")


def headline(dana: Principal, layer: Layer) -> dict[str, Any]:
    planned = plan(dana, QUESTION, None, layer)
    assert planned is not None
    return spec.to_spec(planned[0], layer)


@pytest.mark.parametrize(("failure", "reason"), [(REFUSED, "refusal"), (CUT_OFF, "max_tokens")])
async def test_a_refused_or_cut_off_orchestrator_falls_back(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey, failure: Response, reason: str
) -> None:
    done = await answer_why_live(ScriptedLLM(failure), dana, QUESTION, layer=layer)
    assert (done.live, done.fallback) == (False, reason) and no_key.calls == [QUESTION]


async def test_a_structured_output_that_fails_validation_falls_back(
    dana: Principal, layer: Layer, database: Database, sandbox: Sandbox, no_key: NoKey
) -> None:
    grouped = {**headline(dana, layer), "group_by": ["peril"]}
    llm = ScriptedLLM(
        calls(("query_metric", grouped)),
        calls(("analyze", {"template": "yoy", "dataset": "d2"})),
        reply("Here is the code: def run(df, params): ..."),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert (done.live, done.fallback) == (False, "invalid_output") and sandbox.calls == []


@pytest.mark.parametrize(
    "finish",
    [
        {"analyses": ["a1"], "documents": []},
        {"analyses": [], "documents": ["memo-25-05#hail-event:1"]},
        {"analyses": []},
        {"datasets": ["d1"], "analyses": [], "documents": []},
    ],
)
async def test_a_finish_naming_what_the_run_didnt_make_falls_back(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey, finish: dict[str, Any]
) -> None:
    done = await answer_why_live(ScriptedLLM(calls(("finish", finish))), dana, QUESTION, layer=layer)
    assert (done.live, done.fallback) == (False, "invalid") and no_key.calls == [QUESTION]


async def test_the_headline_is_the_planned_change_whatever_the_orchestrator_queries(
    dana: Principal, layer: Layer, database: Database, sandbox: Sandbox, no_key: NoKey
) -> None:
    another_quarter = {**headline(dana, layer), "period": "2025-Q1"}
    by_peril = {**headline(dana, layer), "group_by": ["peril"]}
    llm = ScriptedLLM(
        calls(("query_metric", another_quarter), ("query_metric", by_peril)),
        calls(("analyze", {"template": "yoy", "dataset": "d3"})),
        json_reply({"code": YOY_CODE}),
        calls(("finish", {"analyses": ["a1"], "documents": []})),
    )
    done = await answer_why_live(llm, dana, QUESTION, layer=layer, sandbox=sandbox)
    assert done.live and done.result.query is not None and done.result.query.period == Period.quarter(2025, 2)
    assert done.result.draft.claims[0].text.startswith("Paid losses in the West were $35,768,928 in Q2 2025")
    assert done.result.draft.caveats == (NO_DOCUMENT,)
    assert all(principal == dana for principal, _, _ in database.calls) and len(database.calls) == 3
    # The other quarter was looked at, but no sentence rests on it, so it isn't evidence. The planned change's
    # statement and its split's are, with the values they ran with.
    ran = [(statement, tuple(bound)) for _, statement, bound in database.calls]
    assert len(done.result.evidence.rows) == 6 and done.result.statements == [ran[0], ran[2]]
    assert all(bound for _, bound in done.result.statements)


async def test_an_orchestrator_that_stops_without_finishing_falls_back(
    dana: Principal, layer: Layer, database: Database, no_key: NoKey
) -> None:
    done = await answer_why_live(ScriptedLLM(reply("Paid losses rose because of hail.")), dana, QUESTION, layer=layer)
    assert (done.live, done.fallback) == (False, "invalid")


def test_the_plan_keeps_the_no_key_scope(layer: Layer) -> None:
    june = principal_for("june")
    assert plan(june, QUESTION, None, layer) is None
    planned = plan(principal_for("priya"), QUESTION, None, layer)
    assert planned is not None and planned[0].compare_to == "prior_period" and planned[0].group_by == ()
