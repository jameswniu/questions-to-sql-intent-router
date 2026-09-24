import uuid
from collections.abc import Callable, Iterator

import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

from app.web.stream import AskFn
from tests.web.fakes import Answer, Claim, ClientFor, Done, Evidence, Stage, parse_sse, scripted

pytestmark = pytest.mark.integration

Connection = psycopg.Connection[TupleRow]


@pytest.fixture
def request_id(superuser: Connection) -> Iterator[uuid.UUID]:
    made = uuid.uuid4()
    yield made
    for table in ("feedback", "audit_log", "request_log"):
        superuser.execute(f"DELETE FROM ops.{table} WHERE request_id = %s", (made,))


def one_answer(request_id: uuid.UUID) -> AskFn:
    claim = Claim("Colorado had 12 hail claims in Q2 2025.")
    return scripted(
        Stage("route", 3.0),
        Stage("sql", 9.0),
        Evidence("sql", {"sql": "SELECT count(*) FROM sem.v_claims WHERE state = %s", "params": ["CO"]}),
        Stage("verify", 2.0),
        Answer(claim.text, (claim,)),
        Done(str(request_id), 20.0, "quantitative", "answer", (100245, 100246), ("ho-2025",)),
    )


async def test_one_request_writes_one_request_log_row_and_one_audit_row(
    client: httpx.AsyncClient, use_pipeline: Callable[[AskFn], None], request_id: uuid.UUID, superuser: Connection
) -> None:
    use_pipeline(one_answer(request_id))
    question = "How many hail claims did jo@example.com, 612-555-0142, file under policy 4471190?"
    assert (await client.post("/ask", json={"q": question})).status_code == 200

    rows = superuser.execute(
        "SELECT user_id, role_name, route, outcome, refusal_reason, stage_ms, verifier, mode, source,"
        " question_redacted, tokens_in, tokens_out, cache_read_tokens, cost_usd, first_event_ms, total_ms"
        " FROM ops.request_log WHERE request_id = %s",
        (request_id,),
    ).fetchall()
    assert len(rows) == 1
    user_id, role, route, outcome, reason, stage_ms, verifier, mode, source, asked, *usage, first_ms, total_ms = rows[0]
    assert (user_id, role, route, outcome, reason, mode, source) == (
        "dana",
        "u_adj_west",
        "quantitative",
        "answer",
        None,
        "none",
        "ui",
    )
    assert stage_ms == {"route": 3, "sql": 9, "verify": 2}
    assert verifier == {"kept": 1, "cut": 0, "retried": False}
    assert asked == "How many hail claims did [email], [phone], file under policy [number]?"
    assert usage == [0, 0, 0, 0]
    assert 0 <= first_ms <= total_ms

    audit = superuser.execute(
        "SELECT user_id, role_name, route, claim_ids, doc_ids FROM ops.audit_log WHERE request_id = %s", (request_id,)
    ).fetchall()
    assert audit == [("dana", "u_adj_west", "quantitative", [100245, 100246], ["ho-2025"])]


async def test_feedback_is_kept_only_from_the_user_who_asked(
    client: httpx.AsyncClient,
    client_for: ClientFor,
    use_pipeline: Callable[[AskFn], None],
    request_id: uuid.UUID,
    superuser: Connection,
) -> None:
    use_pipeline(one_answer(request_id))
    await client.post("/ask", json={"q": "How many hail claims in Colorado in Q2 2025?"})
    rated = await client.post(
        "/feedback", json={"request_id": str(request_id), "rating": -1, "note": "Ring 612-555-0142"}
    )
    assert rated.status_code == 204
    async with client_for("omar") as someone_else:
        await someone_else.post("/feedback", json={"request_id": str(request_id), "rating": 1})
    votes = superuser.execute(
        "SELECT user_id, rating, note FROM ops.feedback WHERE request_id = %s", (request_id,)
    ).fetchall()
    assert votes == [("dana", -1, "Ring [phone]")]


async def test_a_gate_refusal_from_the_real_pipeline_streams_and_is_logged(
    client: httpx.AsyncClient, superuser: Connection
) -> None:
    events = parse_sse((await client.post("/ask", json={"q": "hello there"})).text)
    assert [name for name, _ in events] == ["stage", "refused", "done"]
    request_id = uuid.UUID(events[-1][1]["request_id"])
    try:
        logged = superuser.execute(
            "SELECT route, outcome, refusal_reason FROM ops.request_log WHERE request_id = %s", (request_id,)
        ).fetchall()
        assert logged == [("refuse", "refused", "chit_chat")]
    finally:
        for table in ("audit_log", "request_log"):
            superuser.execute(f"DELETE FROM ops.{table} WHERE request_id = %s", (request_id,))
