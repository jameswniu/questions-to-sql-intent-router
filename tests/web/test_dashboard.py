import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
from psycopg.rows import TupleRow

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


async def dashboard(client: httpx.AsyncClient, **params: str) -> dict[str, Any]:
    response = await client.get("/api/dashboard", params=params)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    page: dict[str, Any] = response.json()
    return page


def labels(chart: dict[str, Any]) -> list[str]:
    return [bar["label"] for bar in chart["bars"]]


async def test_empty_tables_say_there_are_no_requests_yet(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(DashboardData())
    page = await dashboard(supervisor)
    assert page["requests"] == 0 and page["latency"] == [] and page["evals"] is None
    assert page["by_route"] is page["outcomes"] is page["feedback"] is None
    assert page["verifier"] == {"chart": None, "note": "No answer has been through the verifier yet."}
    assert [tile["value"] for tile in page["tiles"]] == ["0", "None yet", "None", "No ratings yet"]


async def test_populated_tables_fill_every_panel(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(populated())
    page = await dashboard(supervisor)
    assert page["tiles"] == [
        {"label": "Requests", "value": "53"},
        {"label": "Answered", "value": "77%"},
        {"label": "Median first event", "value": "85 ms"},
        {"label": "Rated useful", "value": "12 of 15"},
    ]
    charts = [*page["latency"], page["by_route"], page["outcomes"], page["verifier"]["chart"], page["feedback"]]
    assert [chart["title"] for chart in charts] == [
        "Lookup, 40 requests",
        "Why, 8 requests",
        "Requests by route",
        "Outcomes",
        "Claims checked against the evidence",
        "Ratings",
    ]
    lookup, why = page["latency"]
    assert [(bar["label"], bar["text"], bar["tone"]) for bar in lookup["bars"]] == [
        ("First event, p50", "90 ms", "accent-light"),
        ("First event, p95", "300 ms", "accent-light"),
        ("Total, p50", "420 ms", "accent"),
        ("Total, p95", "1.60 s", "accent"),
    ]
    assert lookup["budget"] == {"value": 2.0, "label": "p95 budget 2 s"}
    assert why["budget"] == {"value": 25.0, "label": "p95 budget 25 s"}
    assert lookup["ticks"][0] == {"value": 0, "label": "0 s"} and lookup["ticks"][-1]["value"] >= 2.0
    assert lookup["description"] == (
        "First event, p50: 90 ms. First event, p95: 300 ms. Total, p50: 420 ms. Total, p95: 1.60 s. p95 budget 2 s."
    )
    outcomes = {bar["label"]: (bar["value"], bar["tone"]) for bar in page["outcomes"]["bars"]}
    assert outcomes == {
        "Answered": (41, "good"),
        "Refused, injection": (7, "bad"),
        "Asked a question back": (5, "accent"),
    }
    assert labels(page["by_route"]) == ["Lookup", "Why", "Not routed"]
    assert page["verifier"]["note"] == "The verifier retried 4 of 41 answers (10%)."
    assert [(bar["label"], bar["value"]) for bar in page["verifier"]["chart"]["bars"]] == [
        ("Claims kept", 120),
        ("Claims cut", 9),
    ]
    assert page["cost"] == {
        "chart": None,
        "note": "Zero so far. Every request ran in no-key mode, which calls no model.",
    }
    assert page["evals"] == {
        "runs": [{"split": "dev", "mode": "none", "date": "2026-09-20", "commit": "abc1234def"}],
        "rows": [{"metric": "routing.accuracy", "values": ["0.950"]}],
    }


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
    page = await dashboard(supervisor)
    shown = labels(page["by_route"]) + labels(page["outcomes"])
    assert shown == ["Routed to clarify", "No match", "Asked a question back"]
    for old in ("Residue", "Clarify", "Asked to clarify"):
        assert old not in shown


async def test_the_source_filter_marks_the_current_view(
    supervisor: httpx.AsyncClient, use_dashboard: Callable[[DashboardData], None]
) -> None:
    use_dashboard(populated())
    page = await dashboard(supervisor, source="eval")
    assert [(f["label"], f["source"], f["href"], f["current"]) for f in page["filters"]] == [
        ("All", None, "/dashboard", False),
        ("UI", "ui", "/dashboard?source=ui", False),
        ("Eval", "eval", "/dashboard?source=eval", True),
        ("Replay", "replay", "/dashboard?source=replay", False),
    ]
    assert (page["source"], page["scope"]) == ("eval", "Last 30 days, Eval requests only.")
    everything = await dashboard(supervisor)
    assert (everything["source"], everything["scope"]) == (None, "Last 30 days, all sources.")
    for path in ("/dashboard", "/api/dashboard"):
        assert (await supervisor.get(path, params={"source": "bogus"})).status_code == 422, path


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
            page = await visitor.get("/dashboard", headers=headers)
            data = await visitor.get("/api/dashboard", headers=headers)
        status[user] = (page.status_code, data.status_code)
        if user == "dana":
            assert page.text == data.text == "The dashboard is for operators only."
    assert status == {"dana": (403, 403), "priya": (200, 200), None: (401, 401)}
    # The page itself reads nothing: only the operator's request for the data read the ops tables.
    assert loads == 1


@pytest.mark.integration
async def test_the_dashboard_reads_the_live_ops_tables(supervisor: httpx.AsyncClient) -> None:
    page = await dashboard(supervisor)
    assert page["scope"] == "Last 30 days, all sources." and len(page["tiles"]) == 4
    assert (await supervisor.get("/dashboard")).status_code == 200


@pytest.mark.integration
async def test_the_dashboard_never_sends_a_question(
    supervisor: httpx.AsyncClient, superuser: psycopg.Connection[TupleRow]
) -> None:
    # Aggregates only: a question in the request log never reaches the dashboard, however it is filtered.
    request_id, asked = uuid.uuid4(), f"How much did claim [number] pay, asked {uuid.uuid4().hex}?"
    superuser.execute(
        "INSERT INTO ops.request_log (request_id, user_id, role_name, route, outcome, total_ms, first_event_ms, mode,"
        " source, question_redacted) VALUES (%s, 'dana', 'u_adj_west', 'lookup', 'answer', 40, 5, 'none', 'ui', %s)",
        (request_id, asked),
    )
    try:
        for params in ({}, {"source": "ui"}):
            response = await supervisor.get("/api/dashboard", params=params)
            assert response.status_code == 200 and response.json()["requests"] >= 1
            assert asked not in response.text and "claim [number]" not in response.text
    finally:
        superuser.execute("DELETE FROM ops.request_log WHERE request_id = %s", (request_id,))
