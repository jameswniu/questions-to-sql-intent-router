from collections.abc import Callable

import httpx
import pytest

from app.identity import principals
from app.web import app as web
from app.web import auth, session
from app.web.stream import AskFn
from tests.web.fakes import PROXY_SECRET, ClientFor, Done, Recorded, scripted, via_proxy

ANSWERED = scripted(Done("5b0c1f3e-2a47-4c1d-9e8f-0a1b2c3d4e5f", 1.0, "lookup", "answer"))


def test_a_cookie_edited_to_name_another_user_is_rejected() -> None:
    value = session.encode(session.start("dana"))
    found = session.decode(value)
    assert found is not None and found.user_id == "dana"
    assert session.decode(value.replace("dana", "priya", 1)) is None
    assert session.decode(value[:-1] + ("0" if value[-1] != "0" else "1")) is None
    assert session.decode("") is None


def test_an_expired_cookie_is_rejected() -> None:
    started = session.start("dana")
    value = session.encode(started)
    assert session.decode(value, now=started.issued + session.MAX_AGE_S - 1) is not None
    assert session.decode(value, now=started.issued + session.MAX_AGE_S + 1) is None


async def test_switching_user_starts_a_new_session(client: httpx.AsyncClient) -> None:
    before = session.decode(client.cookies.get(session.COOKIE))
    response = await client.post("/session", json={"user": "omar"})
    assert response.status_code == 204
    after = session.decode(response.cookies.get(session.COOKIE))
    assert before is not None and after is not None
    assert after.user_id == "omar" and after.session_id != before.session_id
    assert (await client.post("/session", json={"user": "mallory"})).status_code == 404


async def test_the_chat_page_signs_in_the_first_demo_user_and_lists_every_one(client_for: ClientFor) -> None:
    async with client_for(None) as anonymous:
        page = await anonymous.get("/")
        assert page.status_code == 200 and '<div id="root">' in page.text
        started = session.decode(page.cookies.get(session.COOKIE))
        assert started is not None and started.user_id == next(iter(principals()))
        assert "httponly" in page.headers["set-cookie"].lower()
        assert "script-src 'self'" in page.headers["content-security-policy"]
        # The page reads who it asks as from the API, on the session the page just started.
        view = await anonymous.get("/api/session")
    assert view.status_code == 200 and "set-cookie" not in view.headers
    assert view.headers["cache-control"] == "no-store"
    first = principals()[started.user_id]
    assert view.json() == {
        "me": {
            "user_id": first.user_id,
            "name": first.name,
            "title": first.title,
            "db_role": first.db_role,
            "ops": False,
        },
        "demo": True,
        "users": [{"user_id": p.user_id, "name": p.name, "title": p.title} for p in principals().values()],
    }


async def test_the_session_view_signs_in_a_new_demo_visitor_too(client_for: ClientFor) -> None:
    # The dev server serves the page itself, so the API starts the demo session the way the page does.
    async with client_for(None) as anonymous:
        view = await anonymous.get("/api/session")
    started = session.decode(view.cookies.get(session.COOKIE))
    assert view.status_code == 200 and started is not None
    assert view.json()["me"]["user_id"] == started.user_id == next(iter(principals()))


async def test_only_an_operator_is_told_they_may_open_the_dashboard(client_for: ClientFor) -> None:
    for user, ops in (("dana", False), ("sam", False), ("priya", True)):
        async with client_for(user) as signed_in:
            me = (await signed_in.get("/api/session")).json()["me"]
        assert (me["user_id"], me["ops"]) == (user, ops)


async def test_behind_the_proxy_there_is_no_switcher(client: httpx.AsyncClient) -> None:
    web.app.state.identity_mode = "header"
    response = await client.post("/session", json={"user": "priya"}, headers=via_proxy("dana"))
    assert response.status_code == 404 and "set-cookie" not in response.headers
    page = await client.get("/", headers=via_proxy("dana"))
    assert page.status_code == 200
    view = await client.get("/api/session", headers=via_proxy("dana"))
    assert view.status_code == 200
    body = view.json()
    assert (body["demo"], body["users"]) == (False, [])
    assert (body["me"]["name"], body["me"]["title"]) == ("Dana Reyes", "Claims adjuster, West")
    assert "Priya" not in view.text


async def test_behind_the_proxy_a_request_without_a_known_user_is_refused(
    client: httpx.AsyncClient, use_pipeline: Callable[[AskFn], None], recorded: Recorded
) -> None:
    # The client holds a valid session cookie for Dana, which alone must not be enough, even through the proxy.
    web.app.state.identity_mode = "header"
    use_pipeline(ANSWERED)
    for headers in (via_proxy(None), via_proxy(""), via_proxy("mallory")):
        response = await client.post("/ask", json={"q": "How many claims?"}, headers=headers)
        assert response.status_code == 401 and response.headers["content-type"].startswith("text/plain")
        assert (await client.get("/", headers=headers)).status_code == 401
        assert (await client.get("/api/session", headers=headers)).status_code == 401
    assert recorded.records == []


async def test_behind_the_proxy_the_header_decides_who_is_asking(
    client: httpx.AsyncClient, use_pipeline: Callable[[AskFn], None], recorded: Recorded
) -> None:
    web.app.state.identity_mode = "header"
    use_pipeline(ANSWERED)
    response = await client.post("/ask", json={"q": "How many claims?"}, headers=via_proxy("omar"))
    assert response.status_code == 200
    [record] = recorded.records
    assert (record.user_id, record.role_name) == ("omar", "u_adj_east")


def test_the_identity_mode_defaults_to_the_proxy_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IDENTITY_MODE", raising=False)
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.setenv("AUTH_PROXY_SECRET", PROXY_SECRET)
    assert auth.identity_mode() == "header"
    monkeypatch.setenv("IDENTITY_MODE", "sso")
    with pytest.raises(ValueError, match="demo or header"):
        auth.identity_mode()


async def test_demo_mode_refuses_to_start_on_a_non_loopback_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IDENTITY_MODE", "demo")
    monkeypatch.setenv("BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("DEMO_ALLOW_NON_LOOPBACK", raising=False)
    with pytest.raises(auth.UnsafeDemo, match="0.0.0.0"):
        async with web.lifespan(web.app):
            pass
    monkeypatch.setenv("DEMO_ALLOW_NON_LOOPBACK", "1")
    assert auth.identity_mode() == "demo"
    monkeypatch.delenv("DEMO_ALLOW_NON_LOOPBACK")
    for host in ("127.0.0.1", "::1", "localhost"):
        monkeypatch.setenv("BIND_HOST", host)
        assert auth.identity_mode() == "demo"
    # An unknown bind is not assumed to be loopback.
    monkeypatch.delenv("BIND_HOST")
    with pytest.raises(auth.UnsafeDemo, match="not set"):
        auth.identity_mode()
