import os
import re
import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
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
FINISH_REASONS = "gen_ai.response.finish_reasons"
PROVIDERS = {"anthropic": "anthropic", "vertex": "gcp.vertex_ai"}
# A model call's prompt template and the hash of its fixed parts: the system prompt, the tool schemas and the
# template's id, never what the user asked.
TEMPLATE = "claims_qa.template"
TEMPLATE_HASH = "claims_qa.template_hash"
# What the request span says about its model calls. Each call's usage stays on its own GenAI span, so a trace
# backend that sums GenAI usage over a trace never counts a request twice.
MODELS = "claims_qa.models"
TEMPLATE_HASHES = "claims_qa.template_hashes"
LIVE_FALLBACK = "claims_qa.live.fallback"

# List prices in USD per million tokens: input, output, cache read. Claude's cache writes bill at 1.25x input.
# Vertex bills Claude through Google, so a Vertex cost here is an estimate at list price.
PRICES = {
    "claude-opus-5-5": (Decimal("4"), Decimal("20"), Decimal("0.20")),
    "claude-opus-5": (Decimal("5"), Decimal("25"), Decimal("0.50")),
    "claude-sonnet-5": (Decimal("2"), Decimal("10"), Decimal("0.20")),
    "claude-haiku-4-5": (Decimal("1"), Decimal("5"), Decimal("0.10")),
    # Vertex's introductory price on the global endpoint, through 2026-12-31. It then rises to 1.50, 7.50 and 0.15.
    "gemini-3.8-flash": (Decimal("0.75"), Decimal("3.75"), Decimal("0.075")),
    # Vertex's global-endpoint price. Output includes the reasoning tokens Gemini thinks with.
    "gemini-3.5-flash": (Decimal("1.50"), Decimal("9.00"), Decimal("0.15")),
}
CACHE_WRITE_RATE = Decimal("1.25")
# A dated snapshot id, such as claude-haiku-4-5-20251001, prices as its undated name.
DATED_SUFFIX = re.compile(r"-\d{8}$")


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    # None once any call used a model with no known price, so an unknown cost never reads as zero.
    cost_usd: Decimal | None = Decimal(0)
    cache_write: int = 0
    # The models that answered, as each reply named itself, and each template called with its hash, in order.
    models: list[str] = field(default_factory=list)
    templates: dict[str, str] = field(default_factory=dict)

    def add(self, attrs: Mapping[str, Any]) -> None:
        total_in, out = int(attrs.get(INPUT_TOKENS, 0)), int(attrs.get(OUTPUT_TOKENS, 0))
        read, written = int(attrs.get(CACHE_READ_TOKENS, 0)), int(attrs.get(CACHE_WRITE_TOKENS, 0))
        self.tokens_in += total_in
        self.tokens_out += out
        self.cache_read += read
        self.cache_write += written
        answered = attrs.get(RESPONSE_MODEL)
        if answered and str(answered) not in self.models:
            self.models.append(str(answered))
        template, digest = attrs.get(TEMPLATE), attrs.get(TEMPLATE_HASH)
        if template and digest:
            self.templates[str(template)] = str(digest)
        model = str(attrs.get(RESPONSE_MODEL) or attrs.get(REQUEST_MODEL) or "").partition("@")[0]
        price = PRICES.get(model)
        if price is None:
            price = PRICES.get(DATED_SUFFIX.sub("", model))
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


def request_attributes(usage: Usage, fallback: str | None) -> dict[str, AttributeValue]:
    """The request span's summary of its model calls: the models that answered, each template called with its
    hash, and why live mode fell back, if it did. Empty for a request that called no model and didn't fall back."""
    attributes: dict[str, AttributeValue] = {}
    if usage.models:
        attributes[MODELS] = list(usage.models)
    if usage.templates:
        attributes[TEMPLATE_HASHES] = [f"{template}={digest}" for template, digest in usage.templates.items()]
    if fallback:
        attributes[LIVE_FALLBACK] = fallback
    return attributes


@dataclass(frozen=True)
class ModelCall:
    span: Span

    def record_template(self, template: str, digest: str) -> None:
        self.span.set_attributes({TEMPLATE: template, TEMPLATE_HASH: digest})

    def record_usage(
        self,
        input_tokens: int,
        output_tokens: int,
        *,
        cache_read: int = 0,
        cache_write: int = 0,
        response_model: str | None = None,
        finish_reason: str | None = None,
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
        if finish_reason:
            attributes[FINISH_REASONS] = [finish_reason]
        self.span.set_attributes(attributes)


@contextmanager
def model_call(model: str, operation: str = "chat", *, provider: str | None = None) -> Iterator[ModelCall]:
    """A GenAI client span around one model request. Nothing from the prompt or the reply goes on it."""
    attributes: dict[str, AttributeValue] = {
        OPERATION: operation,
        PROVIDER: provider or PROVIDERS.get(settings().backend, "anthropic"),
        REQUEST_MODEL: model,
    }
    with tracer().start_as_current_span(f"{operation} {model}", kind=SpanKind.CLIENT, attributes=attributes) as span:
        yield ModelCall(span)
