from collections.abc import Iterator
from decimal import Decimal

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import telemetry
from app.identity import principal_for
from app.requestlog import RequestTrace
from tests.web.fakes import Done, Evidence, Stage

REQUEST_ID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    captured = InMemorySpanExporter()
    telemetry.configure().add_span_processor(SimpleSpanProcessor(captured))
    return captured


@pytest.fixture
def spans(exporter: InMemorySpanExporter) -> Iterator[InMemorySpanExporter]:
    exporter.clear()
    yield exporter
    exporter.clear()


def _everything_recorded(spans: tuple[ReadableSpan, ...]) -> str:
    return repr(
        [(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events]) for s in spans]
    )


def test_spans_carry_stages_and_parameterized_sql_but_never_the_question_or_values(
    spans: InMemorySpanExporter,
) -> None:
    trace = RequestTrace(
        principal_for("dana"), "What did claim 734519 pay? Ask jo@example.com", source="eval", mode="none"
    )
    trace.observe("stage", Stage("route", 5.0))
    statement = "SELECT paid_total FROM sem.v_claim_detail WHERE claim_id = 734519 AND state = %s"
    trace.observe("evidence", Evidence("sql", {"sql": statement, "params": ["CO-SECRET"]}))
    trace.observe("done", Done(REQUEST_ID, 9.0, "lookup", "answer"))
    trace.finish()
    finished = spans.get_finished_spans()
    recorded = _everything_recorded(finished)
    for secret in ("734519", "jo@example.com", "CO-SECRET", "What did"):
        assert secret not in recorded
    root = next(s for s in finished if s.name == "claims_qa.ask")
    stage = next(s for s in finished if s.name == "stage route")
    assert stage.parent is not None and root.context is not None
    assert stage.parent.span_id == root.context.span_id
    assert stage.end_time is not None and stage.start_time is not None
    assert stage.end_time - stage.start_time == 5_000_000
    [query] = root.events
    assert query.attributes is not None
    assert (
        query.attributes["db.query.text"]
        == "SELECT paid_total FROM sem.v_claim_detail WHERE claim_id = %s AND state = %s"
    )
    assert root.attributes is not None
    assert (root.attributes["claims_qa.outcome"], root.attributes["claims_qa.route"]) == ("answer", "lookup")


def test_model_usage_on_genai_spans_sums_into_the_request_with_its_cost(spans: InMemorySpanExporter) -> None:
    trace = RequestTrace(principal_for("omar"), "Why did water claims spike?", source="eval", mode="anthropic")

    def call_model() -> None:
        with telemetry.model_call("claude-sonnet-5") as call:
            call.record_usage(1000, 200, cache_read=4000)
        with telemetry.model_call("claude-sonnet-5") as call:
            call.record_usage(500, 100)

    trace.run_context().run(call_model)
    record = trace.finish()
    assert (record.tokens_in, record.tokens_out, record.cache_read_tokens) == (5500, 300, 4000)
    # 1,500 fresh input at $2, 4,000 cached at $0.20 and 300 output at $10, per million tokens.
    assert record.cost_usd == Decimal("0.006800")
    model_spans = [s for s in spans.get_finished_spans() if s.name == "chat claude-sonnet-5"]
    assert len(model_spans) == 2
    attributes = dict(model_spans[0].attributes or {})
    assert attributes[telemetry.OPERATION] == "chat"
    assert attributes[telemetry.PROVIDER] in ("anthropic", "gcp.vertex_ai")
    assert attributes[telemetry.REQUEST_MODEL] == "claude-sonnet-5"
    assert attributes[telemetry.INPUT_TOKENS] == 5000
    assert not any("message" in key or "prompt" in key or "completion" in key for key in attributes)


def test_a_model_without_a_price_leaves_the_cost_unknown(spans: InMemorySpanExporter) -> None:
    trace = RequestTrace(principal_for("omar"), "Why?", source="replay", mode="anthropic")

    def call_model() -> None:
        with telemetry.model_call("some-unpriced-model") as call:
            call.record_usage(10, 10)

    trace.run_context().run(call_model)
    record = trace.finish()
    assert record.tokens_in == 10
    assert record.cost_usd is None


def test_a_dated_model_id_prices_from_its_undated_name(spans: InMemorySpanExporter) -> None:
    trace = RequestTrace(principal_for("omar"), "Why?", source="eval", mode="anthropic")

    def call_model() -> None:
        with telemetry.model_call("claude-haiku-4-5") as call:
            call.record_usage(1000, 200, response_model="claude-haiku-4-5-20251001")

    trace.run_context().run(call_model)
    record = trace.finish()
    # 1,000 input at $1 and 200 output at $5, per million tokens.
    assert record.cost_usd == Decimal("0.002000")


def test_a_dated_but_otherwise_unpriced_model_still_leaves_the_cost_unknown(spans: InMemorySpanExporter) -> None:
    trace = RequestTrace(principal_for("omar"), "Why?", source="replay", mode="anthropic")

    def call_model() -> None:
        with telemetry.model_call("some-unpriced-model") as call:
            call.record_usage(10, 10, response_model="some-unpriced-model-20251001")

    trace.run_context().run(call_model)
    record = trace.finish()
    assert record.cost_usd is None


def test_no_model_calls_cost_exactly_zero(spans: InMemorySpanExporter) -> None:
    record = RequestTrace(principal_for("sam"), "How many claims?", source="ui", mode="none").finish()
    assert (record.tokens_in, record.tokens_out, record.cost_usd) == (0, 0, Decimal("0.000000"))
