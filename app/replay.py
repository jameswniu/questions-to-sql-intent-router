import asyncio
import json
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import TupleRow

from app import db, pipeline
from app import events as ev
from app.config import ROOT, Backend
from app.identity import Principal, principal_for, role_conninfo
from app.memory import Memory
from app.requestlog import RequestRecord, Source, write_request
from app.web.stream import answer_stream

DEV_CASES = ROOT / "evals" / "cases"
REPLAYED = ("routing.jsonl", "quantitative.jsonl")


@dataclass(frozen=True)
class Logged:
    events: list[ev.Event]
    # What a browser would have received for the same question, decoded from the server-sent events.
    sent: list[dict[str, Any]]
    record: RequestRecord | None
    memory: Memory
    session_id: str


async def ask_logged(principal: Principal, question: str, *, source: Source) -> Logged:
    """One question asked the way the web route asks it, with no model, so its request_log row and the events a
    browser would get are the real ones. The pipeline's own events are kept beside them for scoring."""
    memory, session_id = Memory(), uuid.uuid4().hex
    events: list[ev.Event] = []
    records: list[RequestRecord] = []

    async def ask(
        principal: Principal, question: str, session_id: str, *, backend: Backend = "none"
    ) -> AsyncIterator[object]:
        async for event in pipeline.ask(principal, question, session_id, backend=backend, memory=memory):
            events.append(event)
            yield event

    async def record(row: RequestRecord) -> None:
        records.append(row)
        await write_request(row)

    stream = answer_stream(ask, principal, question, session_id, backend="none", record=record, source=source)
    sent = [json.loads(item.raw_data or "null") async for item in stream]
    return Logged(events, sent, records[0] if records else None, memory, session_id)


def dev_questions() -> list[tuple[str, str]]:
    asked = []
    for name in REPLAYED:
        for line in (DEV_CASES / name).read_text().splitlines():
            case = json.loads(line) if line.strip() else None
            if case is not None and case["split"] == "dev":
                asked.append((case["user"], case["q"]))
    return asked


_DELETE_AUDIT = (
    "DELETE FROM ops.audit_log WHERE request_id IN (SELECT request_id FROM ops.request_log WHERE source = 'replay')"
)
_DELETE_REQUESTS = "DELETE FROM ops.request_log WHERE source = 'replay'"


def _clear_replay_rows() -> None:
    """Deletes old source='replay' rows as the database owner, in one transaction of its own, so a request row and
    its audit row go together. The app's writer role can only insert into the ops tables, so the audit log stays
    append-only for the app itself."""
    conninfo = role_conninfo("postgres", os.environ["POSTGRES_PASSWORD"])
    with psycopg.connect(conninfo, autocommit=True) as conn, conn.transaction():
        conn.execute(_DELETE_AUDIT)
        conn.execute(_DELETE_REQUESTS)


async def _replay(questions: list[tuple[str, str]]) -> Counter[str]:
    outcomes: Counter[str] = Counter()
    try:
        for user, question in questions:
            logged = await ask_logged(principal_for(user), question, source="replay")
            outcomes[logged.record.outcome if logged.record else "unlogged"] += 1
    finally:
        await db.close_all()
    return outcomes


def replay(conn: psycopg.Connection[TupleRow]) -> str:
    """Asks the DEV routing and quantitative questions once, as their users, so /dashboard has traffic on a fresh
    database. Each row is committed as it is written, so old replay rows are cleared on their own connection
    before asking, and again if a run fails partway, which leaves nothing to count twice and lets the next
    bootstrap retry the step from a clean slate."""
    _clear_replay_rows()
    try:
        outcomes = asyncio.run(_replay(dev_questions()))
    except BaseException:
        _clear_replay_rows()
        raise
    return ", ".join(f"{outcome} {count}" for outcome, count in sorted(outcomes.items()))
