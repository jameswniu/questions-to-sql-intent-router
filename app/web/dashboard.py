from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from functools import cache
from typing import Any

import yaml
from markupsafe import Markup

from app import db
from app.semantic.layer import LAYER_PATH
from app.web.charts import ACCENT, ACCENT_LIGHT, GOOD, MUTED, REFUSAL, Bar, Budget, hbar

WINDOW_DAYS = 30
SOURCES = {"ui": "UI", "eval": "Eval", "replay": "Replay"}
ROUTE_ORDER = {
    "lookup": "Lookup",
    "quantitative": "Quantitative",
    "qualitative": "Qualitative",
    "why": "Why",
    "refuse": "Refused at the gate",
    # A question after a figure that doesn't say which one or for when, which the router sends to be asked back.
    "clarify": "Routed to clarify",
    "out_of_data": "Out of data",
    # Stored as residue: no rule matched, and the reply lists what can be asked.
    "residue": "No match",
}
# The pipeline's outcomes, plus the two the web layer adds: error when the pipeline raised, cancelled when the
# client left first. A reply that asks a question back can come from any route, so the clarify outcome counts
# more requests than the clarify route does.
OUTCOMES = {
    "answer": "Answered",
    "not_found": "Not found",
    "not_allowed": "Not allowed",
    "clarify": "Asked a question back",
    "out_of_data": "Outside the data",
    "timeout": "Timed out",
    "unavailable": "Data unavailable",
    "error": "Failed",
    "cancelled": "Cancelled",
}
ANSWERED = "answer"
NOT_SERVED = {"timeout", "unavailable", "error", "cancelled", "unknown"}


@dataclass(frozen=True)
class RouteStats:
    route: str | None
    requests: int
    total_p50: float | None
    total_p95: float | None
    first_p50: float | None
    first_p95: float | None
    live_requests: int
    cost_avg: Decimal | None


@dataclass(frozen=True)
class OutcomeCount:
    outcome: str | None
    refusal_reason: str | None
    requests: int


@dataclass(frozen=True)
class VerifierStats:
    answers: int = 0
    kept: int = 0
    cut: int = 0
    retried: int = 0


@dataclass(frozen=True)
class EvalRun:
    at: datetime
    split: str
    mode: str
    git_commit: str | None
    metrics: Mapping[str, Any]


@dataclass(frozen=True)
class DashboardData:
    routes: Sequence[RouteStats] = ()
    outcomes: Sequence[OutcomeCount] = ()
    verifier: VerifierStats = VerifierStats()
    feedback: Mapping[int, int] = field(default_factory=dict)
    evals: Sequence[EvalRun] = ()
    first_event_p50: float | None = None

    @property
    def requests(self) -> int:
        return sum(route.requests for route in self.routes)


_WINDOW = "at >= now() - make_interval(days => %(days)s::int) AND (%(source)s::text IS NULL OR source = %(source)s)"
_DONE = "outcome IS DISTINCT FROM 'cancelled'"
_ROUTES = f"""
SELECT route, count(*) AS requests,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY total_ms) FILTER (WHERE {_DONE}) AS total_p50,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY total_ms) FILTER (WHERE {_DONE}) AS total_p95,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY first_event_ms) AS first_p50,
    percentile_cont(0.95) WITHIN GROUP (ORDER BY first_event_ms) AS first_p95,
    count(*) FILTER (WHERE mode IS DISTINCT FROM 'none') AS live_requests,
    avg(cost_usd) FILTER (WHERE mode IS DISTINCT FROM 'none') AS cost_avg
FROM ops.request_log WHERE {_WINDOW} GROUP BY route"""
_FIRST_EVENT = f"""
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY first_event_ms) AS p50 FROM ops.request_log WHERE {_WINDOW}"""
_OUTCOMES = f"""
SELECT outcome, refusal_reason, count(*) AS requests FROM ops.request_log WHERE {_WINDOW}
GROUP BY outcome, refusal_reason ORDER BY count(*) DESC"""
_VERIFIER = f"""
SELECT count(verifier) AS answers, coalesce(sum((verifier ->> 'kept')::int), 0) AS kept,
    coalesce(sum((verifier ->> 'cut')::int), 0) AS cut,
    count(*) FILTER (WHERE (verifier ->> 'retried')::boolean) AS retried
FROM ops.request_log WHERE {_WINDOW}"""
# The latest rating each user gave each request, so changing a vote does not count it twice.
_FEEDBACK = """
SELECT rating, count(*) AS votes FROM (
    SELECT DISTINCT ON (f.request_id, f.user_id) f.rating
    FROM ops.feedback f JOIN ops.request_log r USING (request_id)
    WHERE f.at >= now() - make_interval(days => %(days)s::int) AND (%(source)s::text IS NULL OR r.source = %(source)s)
    ORDER BY f.request_id, f.user_id, f.at DESC
) latest GROUP BY rating"""
_EVALS = "SELECT DISTINCT ON (split) at, split, mode, git_commit, metrics FROM ops.eval_runs ORDER BY split, at DESC"


async def load(source: str | None = None) -> DashboardData:
    params = {"days": WINDOW_DAYS, "source": source}
    first = await db.read_ops(_FIRST_EVENT, params)
    return DashboardData(
        routes=[RouteStats(**row) for row in await db.read_ops(_ROUTES, params)],
        outcomes=[OutcomeCount(**row) for row in await db.read_ops(_OUTCOMES, params)],
        verifier=VerifierStats(**(await db.read_ops(_VERIFIER, params))[0]),
        feedback={row["rating"]: row["votes"] for row in await db.read_ops(_FEEDBACK, params)},
        evals=[EvalRun(**row) for row in await db.read_ops(_EVALS)],
        first_event_p50=first[0]["p50"] if first else None,
    )


@cache
def budgets() -> dict[str, float]:
    doc = yaml.safe_load(LAYER_PATH.read_text())["budgets"]
    simple, why = float(doc["simple_p95_s"]), float(doc["why_p95_s"])
    return {"lookup": simple, "quantitative": simple, "qualitative": simple, "why": why}


def seconds(value: float) -> str:
    if value < 1:
        return f"{value * 1000:.0f} ms"
    return f"{value:.2f} s" if value < 10 else f"{value:.1f} s"


def tick(value: float) -> str:
    return f"{value:g} s"


def whole(value: float) -> str:
    return f"{value:,.0f}"


def dollars(value: float) -> str:
    return f"${value:.4f}"


def page(data: DashboardData, source: str | None) -> dict[str, Any]:
    """Everything the dashboard template shows, with the charts already drawn."""
    scope = "all sources" if source is None else f"{SOURCES[source]} requests only"
    filters = [{"label": "All", "href": "/dashboard", "current": source is None}] + [
        {"label": label, "href": f"/dashboard?source={key}", "current": source == key} for key, label in SOURCES.items()
    ]
    return {
        "scope": f"Last {WINDOW_DAYS} days, {scope}.",
        "filters": filters,
        "requests": data.requests,
        "tiles": _tiles(data),
        "latency": _latency(data.routes),
        "by_route": _chart("Requests by route", [Bar(_route(r.route), r.requests) for r in _ordered(data.routes)]),
        "outcomes": _chart("Outcomes", _outcome_bars(data.outcomes)),
        "verifier": _verifier(data.verifier),
        "feedback": _feedback(data.feedback),
        "cost": _cost(data.routes),
        "evals": _evals(data.evals),
    }


def _route(route: str | None) -> str:
    return "Not routed" if route is None else ROUTE_ORDER.get(route, route.replace("_", " ").capitalize())


def _ordered(routes: Sequence[RouteStats]) -> list[RouteStats]:
    order = list(ROUTE_ORDER)
    return sorted(routes, key=lambda r: (r.route is None, order.index(r.route) if r.route in order else 99))


def _chart(title: str, bars: list[Bar], x_label: str = "Requests") -> Markup | None:
    if not bars:
        return None
    return Markup(hbar(title, bars, x_label=x_label, value_format=whole, integer=True))


def _tiles(data: DashboardData) -> list[tuple[str, str]]:
    answered = sum(o.requests for o in data.outcomes if o.outcome == ANSWERED)
    votes = sum(data.feedback.values())
    return [
        ("Requests", whole(data.requests)),
        ("Answered", f"{answered / data.requests:.0%}" if data.requests else "None yet"),
        ("Median first event", seconds(data.first_event_p50 / 1000) if data.first_event_p50 is not None else "None"),
        ("Rated useful", f"{data.feedback.get(1, 0)} of {votes}" if votes else "No ratings yet"),
    ]


def _latency(routes: Sequence[RouteStats]) -> list[Markup]:
    drawn = []
    for r in _ordered(routes):
        limit = budgets().get(r.route or "")
        if limit is None or r.total_p50 is None or r.total_p95 is None:
            continue
        first_p50, first_p95 = (r.first_p50 or 0) / 1000, (r.first_p95 or 0) / 1000
        bars = [
            Bar("First event, p50", first_p50, ACCENT_LIGHT),
            Bar("First event, p95", first_p95, ACCENT_LIGHT),
            Bar("Total, p50", r.total_p50 / 1000),
            Bar("Total, p95", r.total_p95 / 1000),
        ]
        budget = Budget(limit, f"p95 budget {limit:g} s")
        title = f"{_route(r.route)}, {whole(r.requests)} {'request' if r.requests == 1 else 'requests'}"
        svg = hbar(title, bars, x_label="Seconds", value_format=seconds, tick_format=tick, budget=budget)
        drawn.append(Markup(svg))
    return drawn


def _outcome_bars(outcomes: Sequence[OutcomeCount]) -> list[Bar]:
    bars = []
    for o in outcomes:
        if o.outcome == "refused":
            bars.append(Bar(f"Refused, {(o.refusal_reason or 'no reason').replace('_', ' ')}", o.requests, REFUSAL))
            continue
        name = o.outcome or "unknown"
        color = GOOD if name == ANSWERED else MUTED if name in NOT_SERVED else ACCENT
        bars.append(Bar(OUTCOMES.get(name, name.replace("_", " ").capitalize()), o.requests, color))
    return bars


def _verifier(stats: VerifierStats) -> dict[str, Any]:
    if not stats.answers:
        return {"chart": None, "note": "No answer has been through the verifier yet."}
    bars = [Bar("Claims kept", stats.kept, GOOD), Bar("Claims cut", stats.cut, REFUSAL)]
    note = f"The verifier retried {stats.retried:,} of {stats.answers:,} answers ({stats.retried / stats.answers:.0%})."
    return {"chart": _chart("Claims checked against the evidence", bars, x_label="Claims"), "note": note}


def _feedback(votes: Mapping[int, int]) -> Markup | None:
    if not votes:
        return None
    bars = [Bar("Useful", votes.get(1, 0), GOOD), Bar("Not useful", votes.get(-1, 0), REFUSAL)]
    return _chart("Ratings", bars, x_label="Answers rated")


def _cost(routes: Sequence[RouteStats]) -> dict[str, Any]:
    priced = [r for r in _ordered(routes) if r.live_requests and r.cost_avg is not None]
    if not any(r.live_requests for r in routes):
        return {"chart": None, "note": "Zero so far. Every request ran in no-key mode, which calls no model."}
    if not priced:
        return {"chart": None, "note": "The model in use has no price in app/telemetry.py, so cost is unknown."}
    bars = [Bar(_route(r.route), float(r.cost_avg or 0)) for r in priced]
    chart = hbar("Average cost per question", bars, x_label="US dollars", value_format=dollars)
    return {"chart": Markup(chart), "note": "Estimated from token counts at Anthropic list prices."}


def _flatten(metrics: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, Mapping):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _metric(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return whole(value)
    if isinstance(value, float):
        return f"{value:.3f}"
    return "" if value is None else str(value)[:40]


def _evals(runs: Sequence[EvalRun], max_rows: int = 24) -> dict[str, Any] | None:
    if not runs:
        return None
    flat = [_flatten(run.metrics) for run in runs]
    names = sorted({name for metrics in flat for name in metrics})[:max_rows]
    headers = [{"split": r.split, "mode": r.mode, "date": f"{r.at:%Y-%m-%d}", "commit": r.git_commit} for r in runs]
    return {"runs": headers, "rows": [(name, [_metric(metrics.get(name)) for metrics in flat]) for name in names]}
