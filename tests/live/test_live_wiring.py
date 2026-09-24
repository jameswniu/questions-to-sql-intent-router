import asyncio
import inspect
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from psycopg.types.json import Jsonb

from app import config, db, handle, pipeline, requestlog, telemetry
from app import events as ev
from app.answer import quant
from app.answer.qual import QualResult, ReadingDate
from app.answer.quant import QuantResult
from app.answer.types import Answer, Claim, Draft, Evidence
from app.answer.why import NO_DOCUMENT, WhyResult, plan_why
from app.config import ROOT, settings
from app.handle import Handled
from app.identity import Principal, principal_for
from app.live import analyze, extract, spec
from app.live import why as live_why
from app.live.errors import NOTES, Fallback, Notes, fell_back
from app.live.residue import route_residue
from app.live.why import answer_why_live
from app.llm import client as live
from app.llm.fake import ScriptedLLM, calls, json_reply, reply
from app.memory import LastTurn, Memory
from app.requestlog import RequestTrace, write_request
from app.semantic.layer import default_layer
from app.semantic.query import Clarify, MetricQuery
from app.web import app as web
from app.web.stream import answer_stream
from tests.live.conftest import QUESTION, YOY_CODE, Database, NoKey, Sandbox
from tests.live.test_live_writer import KEPT, STRETCHED, SUPPORTED, UNSUPPORTED, WORDING, written

REFUSED = reply("", stop_reason="refusal")
RESIDUE = "banana"
FIGURES = "Total paid losses in the West in Q2 2025"
CHECKED = Claim("Nothing here needs a figure checked.", (), ())


@pytest.fixture
def fresh_settings() -> Iterator[None]:
    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.mark.parametrize(
    ("env", "backend"),
    [
        ({}, "none"),
        ({"LLM_BACKEND": "off"}, "none"),
        ({"LLM_BACKEND": " Vertex "}, "vertex"),
        ({"LLM_BACKEND": "anthropic"}, "anthropic"),
    ],
)
def test_the_backend_is_read_from_llm_backend_alone(
    fresh_settings: None, monkeypatch: pytest.MonkeyPatch, env: dict[str, str], backend: str
) -> None:
    monkeypatch.delenv("LLM_BACKEND", raising=False)
    # The old name no longer does anything.
    monkeypatch.setenv("CLAUDE_BACKEND", "vertex")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert settings().backend == backend == config.backend_from({**env, "CLAUDE_BACKEND": "vertex"})


def test_a_backend_that_isnt_one_stops_the_settings_and_the_client_alike(
    fresh_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_BACKEND", "openai")
    with pytest.raises(ValueError, match="LLM_BACKEND must be one of off, anthropic, vertex"):
        settings()
    with pytest.raises(live.LiveConfigError, match="LLM_BACKEND must be one of off, anthropic, vertex"):
        live.from_env()


def test_the_app_and_live_mode_import_without_pandas_which_only_the_sandbox_image_has() -> None:
    code = "import sys; sys.modules['pandas'] = None; import app.web.app, app.pipeline, app.live.why"
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert ran.returncode == 0, ran.stderr[-2000:]


def test_the_old_backend_settings_are_gone_from_the_repo() -> None:
    names = ("CLAUDE_BACKEND", "VERTEX_PROJECT=", "VERTEX_PROJECT:", "us-east5")
    for path in (".env.example", "compose.yaml", "compose.live.yaml", "app/config.py"):
        text = (ROOT / path).read_text()
        assert not [name for name in names if name in text], path


async def asked(question: str, llm: ScriptedLLM | None = None, backend: config.Backend = "none") -> list[ev.Event]:
    events = pipeline.ask(principal_for("dana"), question, "s1", memory=Memory(), llm=llm, backend=backend)
    return [event async for event in events]


class Stub:
    """Stands in for a route's handler: keeps what it was called with and returns what it was given."""

    def __init__(self, handled: Handled) -> None:
        self.handled = handled
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, principal: Principal, question: str, previous: LastTurn | None, *, llm: Any = None
    ) -> Handled:
        self.calls.append({"question": question, "llm": llm})
        return self.handled


def test_every_handler_takes_the_live_model_the_pipeline_passes_it_in_live_mode() -> None:
    for name, route in handle.HANDLERS.items():
        taken = inspect.signature(route).parameters.get("llm")
        assert taken is not None and taken.kind is inspect.Parameter.KEYWORD_ONLY and taken.default is None, name


async def test_the_model_places_a_question_the_rules_left_as_residue(monkeypatch: pytest.MonkeyPatch) -> None:
    why = Stub(Handled("clarify", [ev.Clarify("Which period?", ())]))
    monkeypatch.setitem(handle.HANDLERS, "why", why)
    llm = ScriptedLLM(json_reply({"route": "why", "reason": "asks what drove a figure"}))
    events = await asked(RESIDUE, llm)
    assert why.calls == [{"question": RESIDUE, "llm": llm}]
    done = events[-1]
    assert isinstance(done, ev.Done) and done.route == "why"
    assert not [e for e in events if isinstance(e, ev.Live)]


async def test_a_residue_routing_that_fails_keeps_the_rules_answer_and_says_why() -> None:
    events = await asked(RESIDUE, ScriptedLLM(REFUSED))
    assert [e for e in events if isinstance(e, ev.Live)] == [ev.Live("refusal", False)]
    assert [e for e in events if isinstance(e, ev.Clarify)] == [handle.CAPABILITIES]
    assert isinstance(events[-1], ev.Done) and events[-1].route == "residue"


async def test_a_live_answer_arrives_as_the_verifier_finished_it(monkeypatch: pytest.MonkeyPatch) -> None:
    finished = Answer("Written and checked.", (CHECKED,), (), (), ())
    draft = Draft((CHECKED,), ())
    written_answer = Handled("answer", [], draft, Evidence((), (), (), ()), answer=finished, retried=True)
    handler = Stub(written_answer)
    monkeypatch.setitem(handle.HANDLERS, "quantitative", handler)
    llm = ScriptedLLM()
    events = await asked(FIGURES, llm)
    assert handler.calls == [{"question": FIGURES, "llm": llm}]
    assert [e for e in events if isinstance(e, Answer)] == [finished]
    assert ev.Live(None, True) in events and events.index(ev.Live(None, True)) < events.index(finished)
    # It was verified as it was written, so no verify stage runs after the route's.
    assert "verify" not in [e.name for e in events if isinstance(e, ev.Stage)]


async def test_a_live_fallback_answers_from_the_fixed_workflow_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    fell_back = Handled("answer", [], Draft((CHECKED,), ()), Evidence((), (), (), ()), fallback="time")
    monkeypatch.setitem(handle.HANDLERS, "quantitative", Stub(fell_back))
    events = await asked(FIGURES, ScriptedLLM(), backend="vertex")
    [answer] = [e for e in events if isinstance(e, Answer)]
    assert answer.text == f"{CHECKED.text} {pipeline.LIVE_FALLBACK_TEXT}"
    assert ev.Live("time", False) in events and "verify" in [e.name for e in events if isinstance(e, ev.Stage)]


async def test_without_a_model_the_pipeline_is_the_no_key_one(monkeypatch: pytest.MonkeyPatch) -> None:
    answered = Handled("answer", [], Draft((CHECKED,), ()), Evidence((), (), (), ()))
    monkeypatch.setitem(handle.HANDLERS, "quantitative", Stub(answered))
    assert not [e for e in await asked(FIGURES) if isinstance(e, ev.Live)]
    # A backend named with no model keeps the caveat it always had, and live mode with no fallback adds none.
    [unwired] = [e for e in await asked(FIGURES, backend="vertex") if isinstance(e, Answer)]
    assert unwired.text.endswith("The vertex model isn't connected yet, so this came from the fixed workflow.")
    [wired] = [e for e in await asked(FIGURES, ScriptedLLM(), backend="vertex") if isinstance(e, Answer)]
    assert wired.text == CHECKED.text


def quant_result(kind: quant.Kind, **extra: Any) -> QuantResult:
    return QuantResult(kind=kind, text=extra.pop("text", ""), data_as_of=date(2026, 7, 15), **extra)


UNREAD = Clarify("measure", "Which figure do you want?", ())
FOUND = MetricQuery("paid_losses")


@pytest.fixture
def figures(monkeypatch: pytest.MonkeyPatch) -> list[MetricQuery]:
    """The rules can't read the question; what live extraction reads is answered, and kept here."""
    answered: list[MetricQuery] = []

    async def answer_quant(*_: Any, **__: Any) -> QuantResult:
        return quant_result("clarify", text=UNREAD.question, clarify=UNREAD)

    async def answer_query(principal: Principal, extracted: MetricQuery, *, layer: Any) -> QuantResult:
        answered.append(extracted)
        return quant_result("answer", text="Paid losses were $1.", query=extracted, sql="SELECT 1", params=(1,))

    monkeypatch.setattr(handle, "answer_quant", answer_quant)
    monkeypatch.setattr(handle, "answer_query", answer_query)
    return answered


@pytest.mark.parametrize("route", [handle.quantitative, handle.clarify])
async def test_live_extraction_answers_what_the_rules_couldnt_read(
    figures: list[MetricQuery], route: handle.LiveHandler
) -> None:
    llm = ScriptedLLM(json_reply(spec.to_spec(FOUND, default_layer())))
    handled = await route(principal_for("priya"), "what did we shell out", None, llm=llm)
    assert figures == [FOUND] and handled.outcome == "answer" and handled.fallback is None
    assert handled.draft is not None and handled.draft.claims[0].text == "Paid losses were $1."
    assert [r.template for r in llm.requests] == [extract.TEMPLATE]


@pytest.mark.parametrize("route", [handle.quantitative, handle.clarify])
async def test_live_extraction_that_fails_keeps_the_rules_question_and_says_why(
    figures: list[MetricQuery], route: handle.LiveHandler
) -> None:
    handled = await route(principal_for("priya"), "what did we shell out", None, llm=ScriptedLLM(REFUSED))
    assert figures == [] and handled.outcome == "clarify" and handled.fallback == "refusal"
    assert handled.events == [ev.Clarify(UNREAD.question, ())]
    # Without a model, the rules' question is all there is, and nothing is asked of one.
    assert (await route(principal_for("priya"), "what did we shell out", None)).fallback is None


@pytest.fixture
def extracted(monkeypatch: pytest.MonkeyPatch) -> QualResult:
    """The extractive answer the qualitative route finds for a burst pipe question."""
    evidence = Evidence((), (WORDING,), (), ())
    claim = Claim(WORDING.body.split(". ")[0] + ".", (), (WORDING.chunk_id,))
    found = QualResult(
        "answer", claim.text, Draft((claim,), ()), evidence, (WORDING,), ReadingDate(date(2026, 7, 15), "today", "")
    )

    async def answer_qual(*_: Any, **__: Any) -> QualResult:
        return found

    monkeypatch.setattr(handle, "answer_qual", answer_qual)
    return found


async def test_the_live_qualitative_answer_is_written_checked_and_retried_once(extracted: QualResult) -> None:
    llm = ScriptedLLM(written(KEPT.text, STRETCHED.text), SUPPORTED, UNSUPPORTED, written(KEPT.text))
    handled = await handle.qualitative(principal_for("priya"), "Is a burst pipe covered?", None, llm=llm)
    assert handled.answer is not None and handled.answer.text == KEPT.text and handled.retried
    assert handled.fallback is None and handled.draft == extracted.draft and llm.left == 0
    assert handled.doc_ids == {WORDING.doc_id}


@pytest.mark.parametrize(("step", "reason"), [(REFUSED, "refusal"), (reply("Nothing cites a source."), "invalid")])
async def test_a_live_qualitative_answer_that_fails_leaves_the_extractive_one(
    extracted: QualResult, step: Any, reason: str
) -> None:
    handled = await handle.qualitative(principal_for("priya"), "Is a burst pipe covered?", None, llm=ScriptedLLM(step))
    assert handled.answer is None and handled.fallback == reason and handled.draft == extracted.draft


async def test_the_live_why_answer_sends_its_statements_with_their_values(
    database: Database, sandbox: Sandbox, no_key: NoKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(analyze, "Sandbox", lambda: sandbox)
    planned = plan_why(principal_for("dana"), QUESTION, None, default_layer())
    assert not isinstance(planned, WhyResult)
    by_peril = spec.to_spec(replace(planned[0], group_by=("peril",), compare_to="prior_period"), default_layer())
    llm = ScriptedLLM(
        calls(("query_metric", by_peril)),
        calls(("analyze", {"template": "yoy", "dataset": "d2"})),
        json_reply({"code": YOY_CODE}),
        calls(("finish", {"analyses": ["a1"], "documents": []})),
    )
    handled = await handle.why(principal_for("dana"), QUESTION, None, llm=llm)
    assert handled.answer is not None and handled.fallback is None and not no_key.calls and llm.left == 0
    assert "Hail claims account for" in handled.answer.text and handled.answer.text.endswith(NO_DOCUMENT)
    sent = [e.payload for e in handled.events if isinstance(e, ev.Evidence) and e.kind == "sql"]
    assert sent == [{"sql": statement, "params": list(bound)} for _, statement, bound in database.calls]
    assert len(sent) == 2 and all(payload["params"] for payload in sent)
    # The conversation remembers the query the no-key workflow would, so a follow-up builds on the same one.
    assert handled.turn is not None and handled.turn.query == planned[0]


@pytest.mark.parametrize(
    ("user", "asked"),
    [
        # A ratio, which the analysis sub-agent can't split and the no-key workflow can.
        ("dana", "Why did the denial rate in the West rise in Q2 2025?"),
        # A change the question pins every dimension of, which the no-key workflow decomposes whole.
        ("priya", "Why did paid losses for hail in Texas rise in Q2 2025?"),
    ],
)
async def test_a_change_the_orchestrator_couldnt_split_goes_to_the_no_key_workflow_without_a_model_call(
    no_key: NoKey, user: str, asked: str
) -> None:
    layer, principal = default_layer(), principal_for(user)
    planned = plan_why(principal, asked, None, layer)
    assert not isinstance(planned, WhyResult), "the question has a change to explain"
    llm = ScriptedLLM()
    done = await answer_why_live(llm, principal, asked, layer=layer)
    assert (done.live, done.fallback) == (False, None) and llm.requests == [] and no_key.calls == [asked]


async def test_a_why_fallback_is_noted_before_the_no_key_answer_runs(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(*_: Any, **__: Any) -> WhyResult:
        raise psycopg.OperationalError("the database went away")

    monkeypatch.setattr(live_why, "answer_why", failing)
    notes = Notes()
    token = NOTES.set(notes)
    try:
        with pytest.raises(psycopg.OperationalError):
            await answer_why_live(ScriptedLLM(REFUSED), principal_for("dana"), QUESTION, layer=default_layer())
    finally:
        NOTES.reset(token)
    assert notes.fallback == "refusal"


@pytest.mark.parametrize(("failure", "outcome"), [(psycopg.OperationalError("gone"), "unavailable"), (None, "timeout")])
async def test_a_fallback_is_logged_even_when_the_no_key_answer_then_fails(
    monkeypatch: pytest.MonkeyPatch, failure: Exception | None, outcome: str
) -> None:
    async def fell_back_then_failed(*_: Any, **__: Any) -> Handled:
        fell_back("refusal")
        if failure is not None:
            raise failure
        await asyncio.sleep(30)
        raise AssertionError("the route's budget should have stopped this")

    monkeypatch.setitem(handle.HANDLERS, "quantitative", fell_back_then_failed)
    monkeypatch.setattr(pipeline, "timeout_for", lambda route, live=False: 0.05)
    events = await asked(FIGURES, ScriptedLLM())
    assert [type(e) for e in events] == [ev.Stage, ev.Stage, ev.Live, ev.Error, ev.Done]
    assert events[2] == ev.Live("refusal") and isinstance(events[-1], ev.Done) and events[-1].outcome == outcome


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    captured = InMemorySpanExporter()
    telemetry.configure().add_span_processor(SimpleSpanProcessor(captured))
    return captured


def test_a_live_request_logs_its_fallback_models_and_prompt_hashes(exporter: InMemorySpanExporter) -> None:
    exporter.clear()
    trace = RequestTrace(principal_for("dana"), "Why?", source="eval", mode="vertex")

    async def two_calls() -> None:
        llm = ScriptedLLM(json_reply({"route": "why", "reason": "x"}), REFUSED)
        assert not isinstance(await route_residue(llm, "first"), Fallback)
        assert isinstance(await route_residue(llm, "second"), Fallback)

    trace.run_context().run(asyncio.run, two_calls())
    trace.observe("live", ev.Live("reading", True))
    trace.observe("answer", Answer("Kept.", (CHECKED,), (), (), ()))
    record = trace.finish()
    assert record.fallback == "reading" and record.verifier == {"kept": 1, "cut": 0, "retried": True}
    # The fake answers as the model it was asked for, so both calls name the fast model.
    assert record.models == ["claude-haiku-4-5"] and list(record.prompt_hashes) == ["residue.v1"]
    assert record.tokens_in == 0 and record.cache_write_tokens == 0 and record.cost_usd == Decimal("0.000000")
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "claims_qa.ask")
    attributes = dict(root.attributes or {})
    assert attributes[telemetry.LIVE_FALLBACK] == "reading" and attributes[telemetry.MODELS] == ("claude-haiku-4-5",)
    assert attributes[telemetry.TEMPLATE_HASHES] == (f"residue.v1={record.prompt_hashes['residue.v1']}",)
    model_spans = [s for s in spans if s.name == "chat claude-haiku-4-5"]
    assert len(model_spans) == 2
    for span in model_spans:
        found = dict(span.attributes or {})
        # The provider is the client's own, here the fake's, never read from the settings.
        assert found[telemetry.TEMPLATE] == "residue.v1" and found[telemetry.PROVIDER] == ScriptedLLM().provider
        # Nothing named after a prompt, a message or a completion goes on a model span, only the template's hash.
        assert not any(word in key for key in found for word in ("prompt", "message", "completion"))
    assert {dict(s.attributes or {})[telemetry.FINISH_REASONS] for s in model_spans} == {("end_turn",), ("refusal",)}


def test_a_request_with_no_model_logs_and_traces_as_it_always_did(exporter: InMemorySpanExporter) -> None:
    exporter.clear()
    record = RequestTrace(principal_for("sam"), "How many claims?", source="ui", mode="none").finish()
    assert (record.fallback, record.models, record.prompt_hashes, record.cache_write_tokens) == (None, [], {}, 0)
    [root] = exporter.get_finished_spans()
    assert not [key for key in root.attributes or {} if key.startswith("claims_qa.live") or key == telemetry.MODELS]


async def test_only_a_live_request_writes_live_modes_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    written_rows: list[tuple[str, dict[str, Any]]] = []

    async def write(statement: str, params: dict[str, Any]) -> None:
        written_rows.append((statement, params))

    monkeypatch.setattr(db, "write", write)
    no_key = RequestTrace(principal_for("sam"), "How many claims?", source="ui", mode="none").finish()
    await write_request(no_key)
    trace = RequestTrace(principal_for("sam"), "How many claims?", source="ui", mode="vertex")
    trace.observe("live", ev.Live("time", False))
    await write_request(trace.finish())
    (old, _), (new, params) = written_rows
    # A database built before live mode's columns existed still takes every no-key row.
    assert old is requestlog._INSERT and "fallback" not in old
    assert new is requestlog._INSERT_LIVE and params["fallback"] == "time"
    assert isinstance(params["prompt_hashes"], Jsonb) and params["models"] == []


async def test_a_database_from_before_live_mode_still_gets_the_row_and_its_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried: list[str] = []

    async def write(statement: str, params: dict[str, Any]) -> None:
        tried.append(statement)
        if statement is requestlog._INSERT_LIVE:
            raise psycopg.errors.UndefinedColumn('column "fallback" of relation "request_log" does not exist')

    monkeypatch.setattr(db, "write", write)
    await write_request(RequestTrace(principal_for("sam"), "How many claims?", source="ui", mode="vertex").finish())
    assert tried == [requestlog._INSERT_LIVE, requestlog._INSERT]


async def test_live_mode_reaches_the_request_log_but_never_the_browser() -> None:
    async def ask(
        principal: Principal, question: str, session_id: str, *, backend: str = "none"
    ) -> AsyncIterator[object]:
        yield ev.Stage("route", 1.0)
        yield ev.Live("time", True)
        yield Answer("Kept.", (CHECKED,), (), (), ())
        yield ev.Done("00000000-0000-4000-8000-000000000001", 5.0, "why", "answer")

    records: list[requestlog.RequestRecord] = []

    async def record(row: requestlog.RequestRecord) -> None:
        records.append(row)

    stream = answer_stream(ask, principal_for("dana"), "Why?", "s1", backend="vertex", record=record)
    sent = [(event.event, json.loads(event.raw_data or "{}")) async for event in stream]
    assert [name for name, _ in sent] == ["stage", "answer", "done"]
    [row] = records
    assert (row.fallback, row.mode, row.verifier) == ("time", "vertex", {"kept": 1, "cut": 0, "retried": True})


def test_the_web_app_binds_the_model_only_in_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "default", lambda: None)
    assert web.pipeline() is pipeline.ask
    llm = ScriptedLLM()
    monkeypatch.setattr(live, "default", lambda: llm)
    seen: list[dict[str, Any]] = []

    def run_pipeline(*args: Any, **kwargs: Any) -> None:
        seen.append(kwargs)

    monkeypatch.setattr(web, "run_pipeline", run_pipeline)
    web.pipeline()(principal_for("dana"), "Why?", "s1", backend="vertex")
    assert seen == [{"backend": "vertex", "llm": llm}]
