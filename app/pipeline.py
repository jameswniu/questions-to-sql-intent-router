import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import cache

import psycopg
import yaml

from app import db
from app import events as ev
from app.answer.types import Answer, Draft, Evidence
from app.config import Backend
from app.gate import screen
from app.handle import HANDLERS, Handled
from app.identity import Principal
from app.ingest.embed import ModelsMissing
from app.memory import Memory
from app.route import route
from app.sandbox.client import SandboxUnavailable
from app.semantic.layer import LAYER_PATH
from app.verify import finalize, verify

# Simple routes get this many times their p95 budget before they are stopped.
SIMPLE_HEADROOM = 3.0
# Retrieval embeds the question and reranks candidates, and the first question after a start loads both models.
QUALITATIVE_TIMEOUT_S = 20.0
VERIFY_TIMEOUT_S = 5.0
TIMEOUT_TEXT = "That took too long, so I stopped it. Try a narrower question."
UNAVAILABLE_TEXT = "I can't reach the data right now. Try again in a moment."

MEMORY = Memory()


@cache
def budgets() -> dict[str, float]:
    with LAYER_PATH.open() as fh:
        return {name: float(seconds) for name, seconds in (yaml.safe_load(fh).get("budgets") or {}).items()}


def timeout_for(route_name: str) -> float:
    if route_name == "why":
        return budgets()["why_p95_s"]
    if route_name == "qualitative":
        return QUALITATIVE_TIMEOUT_S
    return budgets()["simple_p95_s"] * SIMPLE_HEADROOM


def _checked(draft: Draft, evidence: Evidence, principal: Principal) -> Answer:
    return finalize(draft, verify(draft, evidence, principal))


def _fallback_caveat(backend: Backend) -> str:
    return f"The {backend} model isn't connected yet, so this came from the fixed workflow."


class _Clock:
    def __init__(self) -> None:
        self.started = self.last = time.perf_counter()

    def lap(self, name: str) -> ev.Stage:
        now = time.perf_counter()
        stage = ev.Stage(name, round((now - self.last) * 1000, 1))
        self.last = now
        return stage

    def total(self) -> float:
        return round((time.perf_counter() - self.started) * 1000, 1)


async def ask(
    principal: Principal,
    question: str,
    session_id: str,
    *,
    backend: Backend = "none",
    memory: Memory | None = None,
) -> AsyncIterator[ev.Event]:
    """Answers one question as the principal, as a stream of events that ends with Done."""
    memory = memory if memory is not None else MEMORY
    request_id = str(uuid.uuid4())
    clock = _Clock()
    # The gate and the router are regular expressions over at most a thousand characters, so they run inline.
    refusal = screen(question)
    yield clock.lap("gate")
    if refusal is not None:
        yield ev.Refused(refusal.reason, refusal.message)
        yield ev.Done(request_id, clock.total(), "refuse", "refused")
        return
    previous = memory.get(principal.user_id, session_id)
    routed = route(question, previous.turn if previous else None)
    yield clock.lap("route")
    try:
        async with asyncio.timeout(timeout_for(routed.route)):
            handled = await HANDLERS[routed.route](principal, question, previous)
    except (TimeoutError, db.QueryTimeout):
        yield ev.Error(routed.route, TIMEOUT_TEXT)
        yield ev.Done(request_id, clock.total(), routed.route, "timeout")
        return
    except (psycopg.OperationalError, ModelsMissing, SandboxUnavailable):
        yield ev.Error(routed.route, UNAVAILABLE_TEXT)
        yield ev.Done(request_id, clock.total(), routed.route, "unavailable")
        return
    yield clock.lap(routed.route)
    if handled.turn is not None:
        memory.put(principal.user_id, session_id, handled.turn)
    for event in handled.events:
        yield event
    async for event in _answer(handled, principal, backend, clock):
        yield event
    audit = (tuple(sorted(handled.claim_ids)), tuple(sorted(handled.doc_ids)))
    yield ev.Done(request_id, clock.total(), routed.route, handled.outcome, *audit)


async def _answer(handled: Handled, principal: Principal, backend: Backend, clock: _Clock) -> AsyncIterator[ev.Event]:
    if handled.draft is not None and handled.evidence is not None:
        draft = handled.draft
        if backend != "none":
            draft = replace(draft, caveats=(*draft.caveats, _fallback_caveat(backend)))
        try:
            answer = await asyncio.wait_for(
                asyncio.to_thread(_checked, draft, handled.evidence, principal), VERIFY_TIMEOUT_S
            )
        except TimeoutError:
            handled.outcome = "timeout"
            yield ev.Error("verify", TIMEOUT_TEXT)
            return
        yield clock.lap("verify")
        yield answer
    elif handled.text is not None:
        yield Answer(handled.text, (), (), (), ())
