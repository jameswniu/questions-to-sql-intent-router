import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from functools import cache
from typing import cast

import psycopg
import yaml

from app import db
from app import events as ev
from app.answer.types import Answer, Draft, Evidence
from app.config import Backend
from app.gate import screen
from app.handle import HANDLERS, Handled, LiveHandler
from app.identity import Principal
from app.ingest.embed import ModelsMissing
from app.live import extract, writer
from app.live import why as live_why
from app.live.errors import NOTES, Fallback, Notes
from app.live.residue import route_residue
from app.llm.client import LLM
from app.memory import LastTurn, Memory
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
LIVE_FALLBACK_TEXT = "The model couldn't answer this one, so this came from the fixed workflow."

MEMORY = Memory()


@cache
def budgets() -> dict[str, float]:
    with LAYER_PATH.open() as fh:
        return {name: float(seconds) for name, seconds in (yaml.safe_load(fh).get("budgets") or {}).items()}


def timeout_for(route_name: str, *, live: bool = False) -> float:
    """How long a route's handler may run. In live mode a route that calls a model gets that call's own deadline on
    top, so when the call falls back, the no-key answer still has its whole budget."""
    if not live:
        if route_name == "why":
            return budgets()["why_p95_s"]
        if route_name == "qualitative":
            return QUALITATIVE_TIMEOUT_S
        return budgets()["simple_p95_s"] * SIMPLE_HEADROOM
    added = {
        "why": live_why.BUDGET_S,
        "qualitative": writer.QUAL_DEADLINE_S,
        "quantitative": extract.DEADLINE_S,
        "clarify": extract.DEADLINE_S,
    }
    return timeout_for(route_name) + added.get(route_name, 0.0)


def _checked(draft: Draft, evidence: Evidence, principal: Principal) -> Answer:
    return finalize(draft, verify(draft, evidence, principal))


def _fallback_caveat(backend: Backend) -> str:
    return f"The {backend} model isn't connected yet, so this came from the fixed workflow."


def _caveated(draft: Draft, handled: Handled, backend: Backend, live: bool) -> Draft:
    """A no-key draft with the caveat that says why no model wrote it: live mode fell back, or a backend is named
    that the pipeline was given no model for."""
    if live:
        return replace(draft, caveats=(*draft.caveats, LIVE_FALLBACK_TEXT)) if handled.fallback else draft
    if backend != "none":
        return replace(draft, caveats=(*draft.caveats, _fallback_caveat(backend)))
    return draft


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
    llm: LLM | None = None,
) -> AsyncIterator[ev.Event]:
    """Answers one question as the principal, as a stream of events that ends with Done. With a model, live mode
    adds it where the rules give up, and every model step falls back to the no-key answer when it doesn't hold."""
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
    placing: str | None = None
    if llm is not None and routed.route == "residue":
        placed = await route_residue(llm, question)
        if isinstance(placed, Fallback):
            placing = placed.reason
        else:
            routed = placed.routed
    yield clock.lap("route")
    notes = Notes(placing)
    try:
        handled = await _handled(routed.route, principal, question, previous, llm, notes)
    except (TimeoutError, db.QueryTimeout):
        if llm is not None and notes.fallback:
            yield ev.Live(notes.fallback)
        yield ev.Error(routed.route, TIMEOUT_TEXT)
        yield ev.Done(request_id, clock.total(), routed.route, "timeout")
        return
    except (psycopg.OperationalError, ModelsMissing, SandboxUnavailable):
        if llm is not None and notes.fallback:
            yield ev.Live(notes.fallback)
        yield ev.Error(routed.route, UNAVAILABLE_TEXT)
        yield ev.Done(request_id, clock.total(), routed.route, "unavailable")
        return
    yield clock.lap(routed.route)
    if handled.turn is not None:
        memory.put(principal.user_id, session_id, handled.turn)
    for event in handled.events:
        yield event
    if llm is not None and (handled.fallback or notes.fallback or handled.retried):
        yield ev.Live(handled.fallback or notes.fallback, handled.retried)
    async for event in _answer(handled, principal, backend, clock, live=llm is not None):
        yield event
    audit = (tuple(sorted(handled.claim_ids)), tuple(sorted(handled.doc_ids)))
    yield ev.Done(request_id, clock.total(), routed.route, handled.outcome, *audit)


async def _handled(
    route_name: str, principal: Principal, question: str, previous: LastTurn | None, llm: LLM | None, notes: Notes
) -> Handled:
    """The route's handler, within its budget. Without a model, the handler and its budget are exactly the no-key
    ones. With one, what live mode notes along the way lands in notes, even when the handler then fails."""
    handler = HANDLERS[route_name]
    if llm is None:
        async with asyncio.timeout(timeout_for(route_name)):
            return await handler(principal, question, previous)
    token = NOTES.set(notes)
    try:
        async with asyncio.timeout(timeout_for(route_name, live=True)):
            return await cast(LiveHandler, handler)(principal, question, previous, llm=llm)
    finally:
        NOTES.reset(token)


async def _answer(
    handled: Handled, principal: Principal, backend: Backend, clock: _Clock, *, live: bool = False
) -> AsyncIterator[ev.Event]:
    if handled.answer is not None:
        # A live answer was verified as it was written, since the verifier gives its writer one retry.
        yield handled.answer
    elif handled.draft is not None and handled.evidence is not None:
        draft = _caveated(handled.draft, handled, backend, live)
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
