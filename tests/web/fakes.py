import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.identity import Principal
from app.requestlog import RequestRecord
from app.web import auth, session
from app.web.stream import AskFn

# What the tests' sign-in proxy sends in X-Auth-Proxy-Secret, and what the app is told to expect.
PROXY_SECRET = "a-long-random-value-only-the-proxy-knows"


# Stand-ins named and shaped like app.pipeline's events. The web layer reads events by class name and field.
@dataclass(frozen=True)
class Stage:
    name: str
    ms: float


@dataclass(frozen=True)
class Evidence:
    kind: str
    payload: Any


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Answer:
    text: str
    claims_kept: tuple[Claim, ...] = ()
    claims_cut: tuple[Claim, ...] = ()
    could_not_confirm: tuple[str, ...] = ()
    citations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class Refused:
    reason: str
    message: str


@dataclass(frozen=True)
class Done:
    request_id: str
    total_ms: float
    route: str | None
    outcome: str
    claim_ids: tuple[int, ...] = ()
    doc_ids: tuple[str, ...] = ()


def scripted(*events: object, then_raise: Exception | None = None) -> AskFn:
    async def ask(
        principal: Principal, question: str, session_id: str, *, backend: str = "none"
    ) -> AsyncIterator[object]:
        for event in events:
            yield event
        if then_raise is not None:
            raise then_raise

    return ask


@dataclass
class Recorded:
    records: list[RequestRecord] = field(default_factory=list)

    async def __call__(self, record: RequestRecord) -> None:
        self.records.append(record)


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in body.split("\n\n"):
        name, data = "message", []
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line.removeprefix("event: ")
            elif line.startswith("data: "):
                data.append(line.removeprefix("data: "))
        if data:
            events.append((name, json.loads("\n".join(data))))
    return events


def cookie_for(user_id: str) -> str:
    return session.encode(session.start(user_id))


def via_proxy(user: str | None, secret: str = PROXY_SECRET) -> dict[str, str]:
    """The headers the sign-in proxy sets: its secret, and the user it signed in, if any."""
    headers = {auth.SECRET_HEADER: secret}
    if user is not None:
        headers[auth.USER_HEADER] = user
    return headers


ClientFor = Callable[[str | None], AbstractAsyncContextManager[httpx.AsyncClient]]
