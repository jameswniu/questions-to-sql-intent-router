from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from app.web import app as web
from app.web import session
from app.web.dashboard import DashboardData
from app.web.ratelimit import RateLimiter
from app.web.stream import AskFn
from tests.web.fakes import BUILT_INDEX, PROXY_SECRET, ClientFor, Recorded, cookie_for


@pytest.fixture(scope="session")
def built_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A stand-in for the browser app's build, so every test serves the same page whether or not the real one is
    built. tests/web/test_built_app.py checks the real one."""
    root = tmp_path_factory.mktemp("web-dist")
    (root / "static").mkdir()
    (root / "index.html").write_text(BUILT_INDEX)
    (root / "static" / "app.js").write_text("export {};\n")
    (root / "static" / "style.css").write_text("body { margin: 0; }\n")
    return root


@pytest.fixture(autouse=True)
def _fresh_app_state(built_app: Path) -> Iterator[None]:
    # Tests run without the lifespan, which is what sets the mode, so they sign in the demo way unless they say not.
    web.app.state.identity_mode = "demo"
    web.app.state.proxy_secret = PROXY_SECRET
    web.app.state.limiter = RateLimiter()
    web.app.state.web_dist = built_app
    yield
    web.app.dependency_overrides.clear()


@pytest.fixture
def recorded() -> Recorded:
    captured = Recorded()
    web.app.dependency_overrides[web.recorder] = lambda: captured
    return captured


@pytest.fixture
def use_pipeline() -> Callable[[AskFn], None]:
    def install(ask: AskFn) -> None:
        web.app.dependency_overrides[web.pipeline] = lambda: ask

    return install


@pytest.fixture
def use_dashboard() -> Callable[[DashboardData], None]:
    def install(data: DashboardData) -> None:
        async def fixed() -> DashboardData:
            return data

        web.app.dependency_overrides[web.dashboard_data] = fixed

    return install


@pytest.fixture
def client_for() -> ClientFor:
    @asynccontextmanager
    async def open_client(user_id: str | None) -> AsyncIterator[httpx.AsyncClient]:
        cookies = {session.COOKIE: cookie_for(user_id)} if user_id else None
        transport = httpx.ASGITransport(app=web.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", cookies=cookies) as client:
            yield client

    return open_client


@pytest.fixture
async def client(client_for: ClientFor) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for("dana") as signed_in:
        yield signed_in


@pytest.fixture
async def supervisor(client_for: ClientFor) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for("priya") as signed_in:
        yield signed_in
