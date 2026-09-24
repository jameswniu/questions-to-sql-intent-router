import os
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind
from opentelemetry.util.types import AttributeValue

from app.config import settings

# GenAI semantic conventions. Prompts and completions are never set on a span.
OPERATION = "gen_ai.operation.name"
PROVIDER = "gen_ai.provider.name"
REQUEST_MODEL = "gen_ai.request.model"
RESPONSE_MODEL = "gen_ai.response.model"
INPUT_TOKENS = "gen_ai.usage.input_tokens"
OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
CACHE_READ_TOKENS = "gen_ai.usage.cache_read.input_tokens"
CACHE_WRITE_TOKENS = "gen_ai.usage.cache_creation.input_tokens"
PROVIDERS = {"anthropic": "anthropic", "vertex": "gcp.vertex_ai"}

# Anthropic list prices in USD per million tokens: input, output, cache read. Cache writes bill at 1.25x input.
# Vertex bills Claude through Google, so a Vertex cost here is an estimate at list price.
PRICES = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("0.20")),
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("0.50")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("0.20")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5"), Decimal("0.10")),
}
CACHE_WRITE_RATE = Decimal("1.25")


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    # None once any call used a model with no known price, so an unknown cost never reads as zero.
    cost_usd: Decimal | None = Decimal(0)

    def add(self, attrs: Mapping[str, Any]) -> None:
        total_in, out = int(attrs.get(INPUT_TOKENS, 0)), int(attrs.get(OUTPUT_TOKENS, 0))
        read, written = int(attrs.get(CACHE_READ_TOKENS, 0)), int(attrs.get(CACHE_WRITE_TOKENS, 0))
        self.tokens_in += total_in
        self.tokens_out += out
        self.cache_read += read
        model = str(attrs.get(RESPONSE_MODEL) or attrs.get(REQUEST_MODEL) or "").partition("@")[0]
        price = PRICES.get(model)
        if price is None or self.cost_usd is None:
            self.cost_usd = None
            return
        rate_in, rate_out, rate_read = price
        fresh = total_in - read - written
        dollars = fresh * rate_in + written * rate_in * CACHE_WRITE_RATE + read * rate_read + out * rate_out
        self.cost_usd += dollars / 1_000_000


class UsageProcessor(SpanProcessor):
    """Sums model usage per trace as spans end, so the request log and the traces cannot disagree."""

    def __init__(self, max_traces: int = 1024) -> None:
        self._lock = threading.Lock()
        self._by_trace: OrderedDict[int, Usage] = OrderedDict()
        self._max_traces = max_traces

    def on_end(self, span: ReadableSpan) -> None:
        attrs = span.attributes or {}
        if span.context is None or (INPUT_TOKENS not in attrs and OUTPUT_TOKENS not in attrs):
            return
        with self._lock:
            usage = self._by_trace.setdefault(span.context.trace_id, Usage())
            self._by_trace.move_to_end(span.context.trace_id)
            usage.add(attrs)
            while len(self._by_trace) > self._max_traces:
                self._by_trace.popitem(last=False)

    def take(self, trace_id: int) -> Usage:
        with self._lock:
            return self._by_trace.pop(trace_id, None) or Usage()


_usage = UsageProcessor()
_provider: TracerProvider | None = None


def configure() -> TracerProvider:
    """One provider per process: OTLP when an endpoint is set, the console when OTEL_DEBUG=1, otherwise no export."""
    global _provider
    if _provider is None:
        provider = TracerProvider(resource=Resource.create({"service.name": "claims-qa"}))
        provider.add_span_processor(_usage)
        if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"):
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        if os.environ.get("OTEL_DEBUG") == "1":
            provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(provider)
        _provider = provider
    return _provider


def tracer() -> trace.Tracer:
    return configure().get_tracer("claims_qa")


def take_usage(trace_id: int) -> Usage:
    """The model usage summed over one trace's spans, removed from the running totals."""
    configure()
    return _usage.take(trace_id)


@dataclass(frozen=True)
class ModelCall:
    span: Span

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_write: int = 0,
        response_model: str | None = None,
    ) -> None:
        """Takes the counts as Anthropic reports them, where input_tokens leaves out cache reads and writes."""
        attributes: dict[str, AttributeValue] = {
            INPUT_TOKENS: input_tokens + cache_read + cache_write,
            OUTPUT_TOKENS: output_tokens,
            CACHE_READ_TOKENS: cache_read,
            CACHE_WRITE_TOKENS: cache_write,
        }
        if response_model:
            attributes[RESPONSE_MODEL] = response_model
        self.span.set_attributes(attributes)


@contextmanager
def model_call(model: str, operation: str = "chat") -> Iterator[ModelCall]:
    """A GenAI client span around one model request. Nothing from the prompt or the reply goes on it."""
    attributes: dict[str, AttributeValue] = {
        OPERATION: operation,
        PROVIDER: PROVIDERS.get(settings().backend, "anthropic"),
        REQUEST_MODEL: model,
    }
    with tracer().start_as_current_span(f"{operation} {model}", kind=SpanKind.CLIENT, attributes=attributes) as span:
        yield ModelCall(span)
