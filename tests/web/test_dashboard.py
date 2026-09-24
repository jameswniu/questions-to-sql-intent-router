import re
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from app.web import app as web
from app.web.dashboard import DashboardData, EvalRun, OutcomeCount, RouteStats, VerifierStats
from tests.web.fakes import ClientFor, via_proxy


def populated() -> DashboardData:
    return DashboardData(
        routes=[
            RouteStats("lookup", 40, 420.0, 1600.0, 90.0, 300.0, 0, None),
            RouteStats("why", 8, 9000.0, 21000.0, 150.0, 400.0, 0, None),
            RouteStats(None, 5, 30.0, 60.0, 20.0, 40.0, 0, None),
        ],
        outcomes=[
            OutcomeCount("answer", None, 41),
            OutcomeCount("refused", "injection", 7),
            OutcomeCount("clarify", None, 5),
        ],
        verifier=VerifierStats(answers=41, kept=120, cut=9, retried=4),
        feedback={1: 12, -1: 3},
        evals=[
            EvalRun(datetime(2026, 9, 20, tzinfo=UTC), "dev", "none", "abc1234def", {"routing": {"accuracy": 0.95}})
        ],
        first_event_p50=85.0,
    )


async def test_empty_tables_render_a_plain_no_requests_yet(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(DashboardData())
    response = await supervisor.get("/dashboard")
    assert response.status_code == 200
    assert "No requests yet" in response.text and "No eval runs yet" in response.text
    assert "<svg" not in response.text


async def test_populated_tables_draw_every_panel(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(populated())
    response = await supervisor.get("/dashboard")
    html = response.text
    for title in ("Lookup, 40 requests", "Why, 8 requests", "Requests by route", "Outcomes", "Ratings"):
        assert f">{title}</text>" in html
    assert ">Claims checked against the evidence</text>" in html
    assert ">p95 budget 2 s</text>" in html and ">p95 budget 25 s</text>" in html
    assert ">Refused, injection</text>" in html and ">Not routed</text>" in html
    assert "retried 4 of 41 answers (10%)" in html
    assert "Zero so far" in html
    assert "routing.accuracy" in html and "0.950" in html and "2026-09-20" in html
    assert min(int(size) for size in re.findall(r'font-size="(\d+)"', html)) >= 14
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def test_route_and_outcome_labels_say_what_each_counts(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    # Stored as clarify and residue. A reply can ask a question back from any route, so the clarify outcome
    # outnumbers the clarify route, and the two need names that don't read as one count.
    routes = [
        RouteStats("clarify", 46, 30.0, 60.0, 20.0, 40.0, 0, None),
        RouteStats("residue", 582, 30.0, 60.0, 20.0, 40.0, 0, None),
    ]
    use_dashboard(DashboardData(routes=routes, outcomes=[OutcomeCount("clarify", None, 721)]))
    html = (await supervisor.get("/dashboard")).text
    for label in ("Routed to clarify", "No match", "Asked a question back"):
        assert f">{label}</text>" in html
    for old in ("Residue", ">Clarify</text>", "Asked to clarify"):
        assert old not in html


async def test_the_source_filter_marks_the_current_view(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(populated())
    html = (await supervisor.get("/dashboard", params={"source": "eval"})).text
    assert '<a href="/dashboard?source=eval" aria-current="page">Eval</a>' in html
    assert "Eval requests only" in html
    assert (await supervisor.get("/dashboard", params={"source": "bogus"})).status_code == 422


@pytest.mark.parametrize("mode", ["demo", "header"])
async def test_only_an_operator_opens_the_dashboard(mode: str, client_for: ClientFor) -> None:
    web.app.state.identity_mode = mode
    loads = 0

    async def counted() -> DashboardData:
        nonlocal loads
        loads += 1
        return DashboardData()

    web.app.dependency_overrides[web.dashboard_data] = counted
    status = {}
    for user in ("dana", "priya", None):
        headers = via_proxy(user) if mode == "header" else {}
        async with client_for(user if mode == "demo" else None) as visitor:
            response = await visitor.get("/dashboard", headers=headers)
        status[user] = response.status_code
        if user == "dana":
            assert response.text == "The dashboard is for operators only."
    assert status == {"dana": 403, "priya": 200, None: 401}
    assert loads == 1


@pytest.mark.integration
async def test_the_dashboard_reads_the_live_ops_tables(supervisor: httpx.AsyncClient) -> None:
    response = await supervisor.get("/dashboard")
    assert response.status_code == 200
    assert "Service dashboard" in response.text
