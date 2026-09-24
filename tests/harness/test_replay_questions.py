import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import TupleRow

from app import bootstrap, replay
from app.identity import Principal
from app.memory import Memory
from app.replay import Logged
from app.requestlog import RequestRecord, Source, write_request
from evals.splits import load

Connection = psycopg.Connection[TupleRow]

# Three fake dev questions, standing in for the real routing/quantitative set so the test controls
# exactly when a failure lands instead of depending on how many real dev cases exist.
FAKE_QUESTIONS = [
    ("dana", "fake replay question one"),
    ("omar", "fake replay question two"),
    ("priya", "fake replay question three"),
]


def test_the_replay_asks_every_dev_routing_and_quantitative_question_and_nothing_else() -> None:
    wanted = [(case["user"], case["q"]) for name in ("routing", "quantitative") for case in load("dev", name)]
    assert replay.dev_questions() == wanted
    heldout = {case["q"] for name in ("routing", "quantitative") for case in load("heldout", name)}
    assert not heldout & {question for _, question in replay.dev_questions()}


def test_the_replay_runs_once_after_ingest() -> None:
    names = [step.name for step in bootstrap.STEPS]
    assert names.index("replay") > names.index("ingest")
    assert next(step for step in bootstrap.STEPS if step.name == "replay").once


def _fake_record(principal: Principal, question: str) -> RequestRecord:
    return RequestRecord(
        request_id=uuid.uuid4(),
        user_id=principal.user_id,
        role_name=principal.db_role,
        route="test",
        outcome="answer",
        refusal_reason=None,
        stage_ms={},
        total_ms=1,
        first_event_ms=1,
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=0,
        cost_usd=None,
        verifier=None,
        mode="none",
        source="replay",
        question_redacted=question,
        claim_ids=[],
        doc_ids=[],
    )


def _replay_row_counts(superuser: Connection) -> tuple[int, int]:
    requests = superuser.execute("SELECT count(*) FROM ops.request_log WHERE source = 'replay'").fetchone()
    audits = superuser.execute(
        "SELECT count(*) FROM ops.audit_log WHERE request_id IN"
        " (SELECT request_id FROM ops.request_log WHERE source = 'replay')"
    ).fetchone()
    assert requests is not None and audits is not None
    return int(requests[0]), int(audits[0])


@pytest.fixture
def clean_replay_rows(superuser: Connection) -> Iterator[None]:
    def _clear() -> None:
        superuser.execute(
            "DELETE FROM ops.audit_log WHERE request_id IN"
            " (SELECT request_id FROM ops.request_log WHERE source = 'replay')"
        )
        superuser.execute("DELETE FROM ops.request_log WHERE source = 'replay'")

    _clear()
    yield
    _clear()


@pytest.mark.integration
def test_a_replay_that_fails_partway_leaves_no_rows_and_a_retry_leaves_one_set(
    superuser: Connection, monkeypatch: pytest.MonkeyPatch, clean_replay_rows: None
) -> None:
    monkeypatch.setattr(replay, "dev_questions", lambda: FAKE_QUESTIONS)
    written: list[uuid.UUID] = []

    async def fails_on_second_write(principal: Principal, question: str, *, source: Source) -> Logged:
        record = _fake_record(principal, question)
        await write_request(record)
        written.append(record.request_id)
        if len(written) == 2:
            raise RuntimeError("simulated mid-replay failure")
        return Logged(events=[], sent=[], record=record, memory=Memory(), session_id=str(record.request_id))

    monkeypatch.setattr(replay, "ask_logged", fails_on_second_write)
    with pytest.raises(RuntimeError, match="simulated mid-replay failure"):
        replay.replay(superuser)

    # The row from the first, successful call is gone too: a partial run leaves nothing behind.
    assert _replay_row_counts(superuser) == (0, 0)

    async def succeeds(principal: Principal, question: str, *, source: Source) -> Logged:
        record = _fake_record(principal, question)
        await write_request(record)
        return Logged(events=[], sent=[], record=record, memory=Memory(), session_id=str(record.request_id))

    monkeypatch.setattr(replay, "ask_logged", succeeds)
    replay.replay(superuser)
    assert _replay_row_counts(superuser) == (len(FAKE_QUESTIONS), len(FAKE_QUESTIONS))

    # A second clean run does not double up on top of the first.
    replay.replay(superuser)
    assert _replay_row_counts(superuser) == (len(FAKE_QUESTIONS), len(FAKE_QUESTIONS))
