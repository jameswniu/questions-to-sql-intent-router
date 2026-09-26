import hmac
from collections.abc import Callable

import httpx
import pytest

from app.web import app as web
from app.web import auth
from app.web.stream import AskFn
from tests.web.fakes import PROXY_SECRET, ClientFor, Done, Recorded, scripted, via_proxy

ANSWERED = scripted(Done("5b0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f", 1.0, "lookup", "answer"))
QUESTION = {"q": "How many claims?"}


@pytest.fixture
def behind_proxy(use_pipeline: Callable[[AskFn], None], recorded: Recorded) -> Recorded:
    web.app.state.identity_mode = "header"
    use_pipeline(ANSWERED)
    return recorded


async def test_without_the_proxy_secret_even_a_known_user_is_refused(
    behind_proxy: Recorded, client_for: ClientFor
) -> None:
    async with client_for(None) as direct:
        for path in ("/", "/dashboard", "/api/session", "/api/dashboard", "/evidence/scan/scan-1", "/no-such-page"):
            response = await direct.get(path, headers={auth.USER_HEADER: "priya"})
            assert (response.status_code, response.text) == (401, web.SIGN_IN["header"]), path
        response = await direct.post("/ask", json=QUESTION, headers={auth.USER_HEADER: "omar"})
    assert response.status_code == 401
    assert behind_proxy.records == []


@pytest.mark.parametrize("secret", ["", "wrong", PROXY_SECRET[:-1], f"{PROXY_SECRET}x", PROXY_SECRET.upper()])
async def test_a_wrong_proxy_secret_is_refused(secret: str, behind_proxy: Recorded, client_for: ClientFor) -> None:
    async with client_for(None) as direct:
        response = await direct.post("/ask", json=QUESTION, headers=via_proxy("omar", secret))
    assert response.status_code == 401
    assert behind_proxy.records == []


async def test_the_right_secret_with_an_unknown_user_is_still_refused(
    behind_proxy: Recorded, client_for: ClientFor
) -> None:
    async with client_for(None) as direct:
        for user in (None, "", "mallory"):
            response = await direct.post("/ask", json=QUESTION, headers=via_proxy(user))
            assert response.status_code == 401, user
    assert behind_proxy.records == []


async def test_the_right_secret_with_a_known_user_is_let_in(behind_proxy: Recorded, client_for: ClientFor) -> None:
    async with client_for(None) as direct:
        response = await direct.post("/ask", json=QUESTION, headers=via_proxy("omar"))
    assert response.status_code == 200
    [record] = behind_proxy.records
    assert record.user_id == "omar"


async def test_the_health_check_and_static_files_need_no_secret(client_for: ClientFor) -> None:
    web.app.state.identity_mode = "header"
    async with client_for(None) as direct:
        health = await direct.get("/healthz")
        style = await direct.get("/static/style.css")
        script = await direct.get("/static/app.js")
    assert health.status_code == 200 and health.json() == {"ok": True}
    assert style.status_code == script.status_code == 200


@pytest.mark.parametrize("secret", [None, ""])
async def test_header_mode_refuses_to_start_without_a_proxy_secret(
    secret: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IDENTITY_MODE", "header")
    if secret is None:
        monkeypatch.delenv("AUTH_PROXY_SECRET", raising=False)
    else:
        monkeypatch.setenv("AUTH_PROXY_SECRET", secret)
    with pytest.raises(auth.NoProxySecret, match="AUTH_PROXY_SECRET"):
        async with web.lifespan(web.app):
            pass
    monkeypatch.setenv("AUTH_PROXY_SECRET", PROXY_SECRET)
    assert auth.identity_mode() == "header"


async def test_demo_mode_ignores_the_proxy_headers(
    client: httpx.AsyncClient, client_for: ClientFor, use_pipeline: Callable[[AskFn], None], recorded: Recorded
) -> None:
    # The demo user signed in is Dana. A header naming Priya changes nothing, even with the proxy's secret beside it.
    use_pipeline(ANSWERED)
    spoofed = via_proxy("priya")
    assert (await client.get("/dashboard", headers=spoofed)).status_code == 403
    assert (await client.get("/api/dashboard", headers=spoofed)).status_code == 403
    assert (await client.get("/api/session", headers=spoofed)).json()["me"]["user_id"] == "dana"
    assert (await client.post("/ask", json=QUESTION, headers=spoofed)).status_code == 200
    [record] = recorded.records
    assert record.user_id == "dana"
    async with client_for(None) as anonymous:
        assert (await anonymous.post("/ask", json=QUESTION, headers=spoofed)).status_code == 401


def test_the_secret_is_compared_in_constant_time(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    assert auth.from_proxy(PROXY_SECRET, PROXY_SECRET)
    assert not auth.from_proxy("wrong", PROXY_SECRET)
    # No secret configured, or none sent, matches nothing.
    assert not auth.from_proxy("", "") and not auth.from_proxy(None, PROXY_SECRET)
    assert compared == [(PROXY_SECRET.encode(), PROXY_SECRET.encode()), (b"wrong", PROXY_SECRET.encode())]
