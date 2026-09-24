from collections.abc import Callable

import httpx
import pytest

from app.web.ratelimit import RateLimiter
from app.web.stream import AskFn
from tests.web.fakes import Done, Recorded, scripted


def test_bucket_allows_twenty_a_minute_then_waits_for_the_next_token() -> None:
    now = [0.0]
    limiter = RateLimiter(20, 60.0, clock=lambda: now[0])
    assert all(limiter.take("dana") == 0 for _ in range(20))
    assert limiter.take("dana") == pytest.approx(3.0)
    now[0] += 3.0
    assert limiter.take("dana") == 0
    assert limiter.take("dana") > 0


def test_each_user_has_their_own_bucket() -> None:
    limiter = RateLimiter(2, 60.0, clock=lambda: 0.0)
    assert [limiter.take("dana") == 0 for _ in range(3)] == [True, True, False]
    assert limiter.take("omar") == 0


async def test_the_twenty_first_question_in_a_minute_gets_a_plain_429(
    client: httpx.AsyncClient, use_pipeline: Callable[[AskFn], None], recorded: Recorded
) -> None:
    use_pipeline(scripted(Done("5b0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f", 1.0, "lookup", "answer")))
    for _ in range(20):
        assert (await client.post("/ask", json={"q": "Show claim 100245"})).status_code == 200
    refused = await client.post("/ask", json={"q": "Show claim 100245"})
    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("text/plain")
    assert refused.text.startswith("You have asked 20 questions in the last minute.")
    assert int(refused.headers["retry-after"]) >= 1
    assert len(recorded.records) == 20
