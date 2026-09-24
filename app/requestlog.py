import contextlib
import contextvars
import logging
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sized
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from functools import cache
from typing import Any, Literal

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue
from psycopg import errors
from psycopg.types.json import Jsonb

from app import db, telemetry
from app.identity import Principal
from app.redact import redact, sql_for_span

log = logging.getLogger(__name__)

Source = Literal["ui", "eval", "replay"]
QUESTION_CAP = 2000

EVENT_KINDS = {
    "Stage": "stage",
    "Evidence": "evidence",
    "Answer": "answer",
    "Refused": "refused",
    "Clarify": "clarify",
    "OutOfData": "out_of_data",
    "Live": "live",
    "Done": "done",
}


def kind_of(event: object) -> str:
    """The pipeline's events by class name; any other event that carries a message is its error event."""
    name = type(event).__name__
    if name in EVENT_KINDS:
        return EVENT_KINDS[name]
    return "error" if hasattr(event, "message") else re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


@dataclass(frozen=True)
class RequestRecord:
    request_id: uuid.UUID
    user_id: str
    role_name: str
    route: str | None
    outcome: str
    refusal_reason: str | None
    stage_ms: dict[str, int]
    total_ms: int
    first_event_ms: int | None
    tokens_in: int
    tokens_out: int
    cache_read_tokens: int
    cost_usd: Decimal | None
    verifier: dict[str, Any] | None
    mode: str
    source: str
    question_redacted: str
    claim_ids: list[int]
    doc_ids: list[str]
    # Live mode only: why it fell back, if it did, what it wrote to the cache, the models that answered as each
    # reply named itself, and each prompt template called with the hash of its fixed parts.
    fallback: str | None = None
    cache_write_tokens: int = 0
    models: list[str] = field(default_factory=list)
    prompt_hashes: dict[str, str] = field(default_factory=dict)


class RequestTrace:
    """Follows one question through the pipeline's events: a span for the request, one per stage, one log row.
    The web route feeds it; an eval or replay runner can feed it the same way with its own source."""

    def __init__(self, principal: Principal, question: str, *, source: Source, mode: str) -> None:
        self.principal, self.question, self.source, self.mode = principal, question, source, mode
        self.request_id = uuid.uuid4()
        self.route: str | None = None
        self.outcome: str | None = None
        self.refusal_reason: str | None = None
        self.stage_ms: dict[str, int] = {}
        self.verifier: dict[str, int] | None = None
        self.retried = False
        self.fallback: str | None = None
        self.claim_ids: list[int] = []
        self.doc_ids: list[str] = []
        self.first_event_ms: int | None = None
        self.done = False
        self._started = time.perf_counter()
        self._record: RequestRecord | None = None
        self._span = telemetry.tracer().start_span(
            "claims_qa.ask",
            kind=SpanKind.SERVER if source == "ui" else SpanKind.INTERNAL,
            attributes={
                "user.id": principal.user_id,
                "claims_qa.db_role": principal.db_role,
                "claims_qa.source": source,
                "claims_qa.mode": mode,
            },
        )

    def run_context(self) -> contextvars.Context:
        """A context where the request span is current, for the task that runs the pipeline."""
        context = contextvars.copy_context()
        context.run(otel_context.attach, trace.set_span_in_context(self._span))
        return context

    def observe(self, kind: str, event: Any) -> None:
        if self.first_event_ms is None:
            self.first_event_ms = self._elapsed_ms()
        match kind:
            case "stage":
                self._stage(str(getattr(event, "name", "stage")), float(getattr(event, "ms", 0) or 0))
            case "evidence" if getattr(event, "kind", None) == "sql":
                statement = _sql_text(getattr(event, "payload", None))
                masked = sql_for_span(statement) if statement else None
                if masked:
                    # The parameterized text only: parameter values never reach a span.
                    self._span.add_event("db.query", {"db.system.name": "postgresql", "db.query.text": masked})
            case "answer":
                self.outcome = "answer"
                kept, cut = _count(getattr(event, "claims_kept", None)), _count(getattr(event, "claims_cut", None))
                self.verifier = {"kept": kept, "cut": cut}
                self.retried = self.retried or getattr(event, "retried", False) is True
            case "live":
                self.fallback = _text(getattr(event, "fallback", None)) or self.fallback
                self.retried = self.retried or getattr(event, "retried", False) is True
            case "refused":
                self.outcome, self.refusal_reason = "refused", _text(getattr(event, "reason", None))
            case "clarify" | "out_of_data":
                self.outcome = kind
            case "done":
                self._done(event)
            case "error":
                self.fail(str(getattr(event, "message", "")))

    def fail(self, reason: str) -> None:
        self.outcome = "error"
        self._span.set_status(Status(StatusCode.ERROR, reason[:200]))

    def cancel(self) -> None:
        if not self.done:
            self.outcome = "cancelled"

    def finish(self) -> RequestRecord:
        if self._record is not None:
            return self._record
        usage = telemetry.take_usage(self._span.get_span_context().trace_id)
        record = RequestRecord(
            request_id=self.request_id,
            user_id=self.principal.user_id,
            role_name=self.principal.db_role,
            route=self.route,
            outcome=self.outcome or "unknown",
            refusal_reason=self.refusal_reason,
            stage_ms=self.stage_ms,
            total_ms=self._elapsed_ms(),
            first_event_ms=self.first_event_ms,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            cache_read_tokens=usage.cache_read,
            cost_usd=None if usage.cost_usd is None else usage.cost_usd.quantize(Decimal("0.000001")),
            verifier=None if self.verifier is None else {**self.verifier, "retried": self.retried},
            mode=self.mode,
            source=self.source,
            question_redacted=redact(self.question)[:QUESTION_CAP],
            claim_ids=self.claim_ids,
            doc_ids=self.doc_ids,
            fallback=self.fallback,
            cache_write_tokens=usage.cache_write,
            models=list(usage.models),
            prompt_hashes=dict(usage.templates),
        )
        attributes: dict[str, AttributeValue | None] = {
            "claims_qa.request_id": str(record.request_id),
            "claims_qa.route": record.route,
            "claims_qa.outcome": record.outcome,
            "claims_qa.refusal_reason": record.refusal_reason,
            "claims_qa.first_event_ms": record.first_event_ms,
            "claims_qa.total_ms": record.total_ms,
            **telemetry.request_attributes(usage, record.fallback),
        }
        self._span.set_attributes({key: value for key, value in attributes.items() if value is not None})
        self._span.end()
        self._record = record
        return record

    def _stage(self, name: str, ms: float) -> None:
        if name in self.stage_ms or "retry" in name:
            self.retried = True
        self.stage_ms[name] = self.stage_ms.get(name, 0) + round(ms)
        # A stage is reported once it has finished, so its span is placed after the fact from its duration.
        end = time.time_ns()
        span = telemetry.tracer().start_span(
            f"stage {name}",
            context=trace.set_span_in_context(self._span),
            start_time=end - int(ms * 1_000_000),
            attributes={"claims_qa.stage": name},
        )
        span.end(end_time=end)

    def _done(self, event: Any) -> None:
        self.done = True
        self.route = _text(getattr(event, "route", None)) or self.route
        self.outcome = _text(getattr(event, "outcome", None)) or self.outcome
        with contextlib.suppress(AttributeError, ValueError):
            self.request_id = uuid.UUID(str(event.request_id))
        self.claim_ids = _ints(getattr(event, "claim_ids", None))
        self.doc_ids = [str(doc) for doc in getattr(event, "doc_ids", None) or ()]

    def _elapsed_ms(self) -> int:
        return round((time.perf_counter() - self._started) * 1000)


_INSERT = """
WITH logged AS (
    INSERT INTO ops.request_log (request_id, user_id, role_name, route, outcome, refusal_reason, stage_ms, total_ms,
        first_event_ms, tokens_in, tokens_out, cache_read_tokens, cost_usd, verifier, mode, source, question_redacted)
    VALUES (%(request_id)s, %(user_id)s, %(role_name)s, %(route)s, %(outcome)s, %(refusal_reason)s, %(stage_ms)s,
        %(total_ms)s, %(first_event_ms)s, %(tokens_in)s, %(tokens_out)s, %(cache_read_tokens)s, %(cost_usd)s,
        %(verifier)s, %(mode)s, %(source)s, %(question_redacted)s)
)
INSERT INTO ops.audit_log (request_id, user_id, role_name, route, claim_ids, doc_ids)
VALUES (%(request_id)s, %(user_id)s, %(role_name)s, %(route)s, %(claim_ids)s::int[], %(doc_ids)s::text[])
"""
# The same, with live mode's columns. Only a request that ran with a model backend writes it, so a database built
# before those columns existed still takes every no-key row, and a live one without its live columns.
_INSERT_LIVE = """
WITH logged AS (
    INSERT INTO ops.request_log (request_id, user_id, role_name, route, outcome, refusal_reason, stage_ms, total_ms,
        first_event_ms, tokens_in, tokens_out, cache_read_tokens, cost_usd, verifier, mode, source, question_redacted,
        fallback, cache_write_tokens, models, prompt_hashes)
    VALUES (%(request_id)s, %(user_id)s, %(role_name)s, %(route)s, %(outcome)s, %(refusal_reason)s, %(stage_ms)s,
        %(total_ms)s, %(first_event_ms)s, %(tokens_in)s, %(tokens_out)s, %(cache_read_tokens)s, %(cost_usd)s,
        %(verifier)s, %(mode)s, %(source)s, %(question_redacted)s, %(fallback)s, %(cache_write_tokens)s,
        %(models)s::text[], %(prompt_hashes)s)
)
INSERT INTO ops.audit_log (request_id, user_id, role_name, route, claim_ids, doc_ids)
VALUES (%(request_id)s, %(user_id)s, %(role_name)s, %(route)s, %(claim_ids)s::int[], %(doc_ids)s::text[])
"""


async def write_request(record: RequestRecord) -> None:
    """The request_log row and its audit_log row, in one statement so neither is written without the other."""
    params = asdict(record)
    params["stage_ms"] = Jsonb(record.stage_ms)
    params["verifier"] = None if record.verifier is None else Jsonb(record.verifier)
    if record.mode == "none":
        await db.write(_INSERT, params)
        return
    try:
        await db.write(_INSERT_LIVE, {**params, "prompt_hashes": Jsonb(record.prompt_hashes)})
    except errors.UndefinedColumn:
        # The schema runs once, at the first bootstrap, so a database from before live mode needs make reset to get
        # its columns. Until then the row and its audit row are still written, without them.
        _warn_without_live_columns()
        await db.write(_INSERT, params)


@cache
def _warn_without_live_columns() -> None:
    log.warning("ops.request_log has no live-mode columns, so live requests are logged without them until make reset")


def _sql_text(payload: object) -> str | None:
    value = payload.get("sql") if isinstance(payload, Mapping) else getattr(payload, "sql", payload)
    return value if isinstance(value, str) else None


def _count(value: object) -> int:
    if isinstance(value, bool | int):
        return int(value)
    return len(value) if isinstance(value, Sized) else 0


def _ints(values: Any) -> list[int]:
    found: list[int] = []
    for value in values if isinstance(values, Iterable) and not isinstance(values, str | bytes) else ():
        with contextlib.suppress(TypeError, ValueError):
            found.append(int(value))
    return found


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
