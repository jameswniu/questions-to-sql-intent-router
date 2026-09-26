"""How the app serves the browser app: the same built page at / and /dashboard, under the page's security headers,
and its files under /static. The server decides who may open each page, before the page is sent."""

from pathlib import Path

import httpx

from app.web import app as web
from tests.web.fakes import ClientFor


def test_the_content_security_policy_is_unchanged() -> None:
    # The React app is built to run under exactly this policy: no inline script or style, nothing from elsewhere.
    assert web.CSP == (
        "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )


async def test_both_pages_are_the_built_app_under_the_security_headers(client_for: ClientFor, built_app: Path) -> None:
    async with client_for("priya") as operator:
        for path in ("/", "/dashboard", "/dashboard?source=ui"):
            response = await operator.get(path)
            assert response.status_code == 200, path
            assert response.text == (built_app / "index.html").read_text()
            assert response.headers["content-type"] == "text/html; charset=utf-8"
            assert response.headers["content-security-policy"] == web.CSP
            assert response.headers["x-content-type-options"] == "nosniff"
            assert response.headers["referrer-policy"] == "same-origin"
            assert response.headers["cache-control"] == "no-cache"


async def test_the_built_files_are_served_and_checked_for_changes_on_every_load(client: httpx.AsyncClient) -> None:
    script = await client.get("/static/app.js")
    assert script.status_code == 200 and script.headers["cache-control"] == "no-cache"
    assert (await client.get("/static/style.css")).headers["content-type"].startswith("text/css")
    for path in ("/static/missing.js", "/static/..%2Findex.html", "/static/%2e%2e/index.html"):
        assert (await client.get(path)).status_code == 404, path


async def test_a_missing_build_says_how_to_build_it(client: httpx.AsyncClient, tmp_path: Path) -> None:
    web.app.state.web_dist = tmp_path / "not-built"
    page = await client.get("/")
    assert page.status_code == 503 and "make frontend-build" in page.text
    assert (await client.get("/static/app.js")).status_code == 404


async def test_only_the_two_pages_are_the_app(client: httpx.AsyncClient) -> None:
    for path in ("/index.html", "/chat", "/api", "/api/nothing"):
        assert (await client.get(path)).status_code == 404, path
