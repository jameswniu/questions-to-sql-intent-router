import asyncio
import dataclasses
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sized
from datetime import date
from decimal import Decimal
from enum import Enum
from functools import partial
from typing import Any, Protocol
from uuid import UUID

import anyio
from fastapi.sse import ServerSentEvent

from app.config import Backend
from app.identity import Principal
from app.requestlog import RequestRecord, RequestTrace, Source, kind_of

log = logging.getLogger(__name__)

PIPELINE_TIMEOUT_S = 120.0
CANCEL_GRACE_S = 5.0
RECORD_TIMEOUT_S = 5.0
TIMEOUT_MESSAGE = "This took longer than two minutes, so it was stopped. Try asking something narrower."
FAILURE_MESSAGE = "Something went wrong on our side and the question was not answered."


class AskFn(Protocol):
    def __call__(
        self, principal: Principal, question: str, session_id: str, *, backend: Backend = "none"
    ) -> AsyncIterator[object]: ...


Recorder = Callable[[RequestRecord], Awaitable[None]]

_END = object()
_FAILED = object()


def fields_of(value: object) -> dict[str, Any] | None:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    as_dict = getattr(value, "_asdict", None) or getattr(value, "model_dump", None)
    if callable(as_dict):
        result = as_dict()
        return dict(result) if isinstance(result, Mapping) else None
    return None


def _json_default(value: object) -> Any:
    found = fields_of(value)
    if found is not None:
        return found
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Decimal | UUID):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(value, key=str)
    return str(value)


def encode(kind: str, body: Mapping[str, Any]) -> ServerSentEvent:
    """One event: the SSE event name is the kind, and the data is the event's fields as JSON with a "type" key."""
    return ServerSentEvent(event=kind, raw_data=json.dumps({**body, "type": kind}, default=_json_default))


async def answer_stream(
    ask: AskFn,
    principal: Principal,
    question: str,
    session_id: str,
    *,
    backend: Backend,
    record: Recorder,
    source: Source = "ui",
    timeout_s: float = PIPELINE_TIMEOUT_S,
) -> AsyncIterator[ServerSentEvent]:
    """One server-sent event per pipeline event. The pipeline runs as its own task, so a client that goes away
    cancels it, and every request ends in one request_log row whether it finished, failed or was cancelled. Every
    ending but a disconnect closes with a done event, so the browser keeps the request id and knows how it ended."""
    request = RequestTrace(principal, question, source=source, mode=backend)
    queue: asyncio.Queue[object] = asyncio.Queue()
    start = partial(ask, principal, question, session_id, backend=backend)
    task = asyncio.create_task(_pump(start, queue), context=request.run_context())
    started = asyncio.get_running_loop().time()
    deadline = started + timeout_s
    try:
        while True:
            try:
                async with asyncio.timeout_at(deadline):
                    item = await queue.get()
            except TimeoutError:
                for event in _stopped(request, started, "timeout", "timed out", TIMEOUT_MESSAGE):
                    yield event
                return
            if item is _END:
                if not request.done:
                    # The pipeline ends every answer with its own done. One that stopped without it failed, unless
                    # it had already answered, refused or asked back, and then only the done is missing.
                    if request.outcome is None:
                        closing = _stopped(
                            request, started, "error", "the pipeline ended without a done", FAILURE_MESSAGE
                        )
                    else:
                        closing = [_done(request, started, request.outcome)]
                    for event in closing:
                        yield event
                return
            if item is _FAILED:
                for event in _stopped(request, started, "error", "the pipeline raised", FAILURE_MESSAGE):
                    yield event
                return
            kind = kind_of(item)
            try:
                request.observe(kind, item)
            except Exception:
                log.exception("could not account for a %s event", kind)
            yield encode(kind, _for_client(kind, item))
    except (asyncio.CancelledError, GeneratorExit):
        request.cancel()
        raise
    finally:
        await _settle(task, request, record)


async def _pump(start: Callable[[], AsyncIterator[object]], queue: asyncio.Queue[object]) -> None:
    try:
        async for event in start():
            queue.put_nowait(event)
    except Exception:
        log.exception("the pipeline raised")
        queue.put_nowait(_FAILED)
    else:
        queue.put_nowait(_END)


async def _settle(task: asyncio.Task[None], request: RequestTrace, record: Recorder) -> None:
    # Shielded, because when the client has gone this generator is being cancelled, and the pipeline still has to
    # stop and the request still has to be logged.
    with anyio.CancelScope(shield=True):
        if not task.done():
            task.cancel()
            finished, _ = await asyncio.wait({task}, timeout=CANCEL_GRACE_S)
            if not finished:
                log.warning("the pipeline for request %s is still running after cancellation", request.request_id)
        try:
            with anyio.fail_after(RECORD_TIMEOUT_S):
                await record(request.finish())
        except Exception:
            log.exception("could not log request %s", request.request_id)


def _for_client(kind: str, event: object) -> dict[str, Any]:
    body = fields_of(event) or {"value": event}
    cut = body.get("claims_cut")
    if kind == "answer" and isinstance(cut, Sized) and not isinstance(cut, str):
        # The verifier keeps a cut claim's words from the asker: its figure was wrong, or its source is not theirs
        # to read. The browser gets the count, and could_not_confirm carries the reasons.
        body["claims_cut"] = len(cut)
    return body


def _error(request: RequestTrace, message: str) -> ServerSentEvent:
    return encode("error", {"message": message, "request_id": str(request.request_id)})


def _stopped(request: RequestTrace, started: float, outcome: str, reason: str, message: str) -> list[ServerSentEvent]:
    """The error the asker reads, then the done that closes the request."""
    request.fail(reason)
    return [_error(request, message), _done(request, started, outcome)]


def _done(request: RequestTrace, started: float, outcome: str) -> ServerSentEvent:
    """A done event in the pipeline's shape, with the outcome the request log records: timeout, or error for a
    pipeline that raised, which the dashboard shows as failed."""
    request.outcome, request.done = outcome, True
    total_ms = round((asyncio.get_running_loop().time() - started) * 1000, 1)
    body = {"request_id": str(request.request_id), "total_ms": total_ms, "route": request.route, "outcome": outcome}
    return encode("done", {**body, "claim_ids": [], "doc_ids": []})
