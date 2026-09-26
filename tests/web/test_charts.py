import json

from app.web.charts import Bar, Budget, hbar, nice_ticks


def seconds(value: float) -> str:
    return f"{value:.2f} s"


def test_a_chart_carries_its_bars_ticks_and_budget_as_the_dashboard_writes_them() -> None:
    chart = hbar(
        "Lookup, 12 requests",
        [Bar("Total, p50", 0.4), Bar("First event, p95", 1.3, "accent-light")],
        x_label="Seconds",
        value_format=seconds,
        budget=Budget(2.0, "p95 budget 2 s"),
    )
    assert (chart["title"], chart["x_label"]) == ("Lookup, 12 requests", "Seconds")
    assert chart["bars"] == [
        {"label": "Total, p50", "value": 0.4, "text": "0.40 s", "tone": "accent"},
        {"label": "First event, p95", "value": 1.3, "text": "1.30 s", "tone": "accent-light"},
    ]
    assert chart["budget"] == {"value": 2.0, "label": "p95 budget 2 s"}
    assert [tick["value"] for tick in chart["ticks"]] == [0, 0.5, 1.0, 1.5, 2.0]
    assert chart["ticks"][1]["label"] == "0.50 s"
    assert chart["description"] == "Total, p50: 0.40 s. First event, p95: 1.30 s. p95 budget 2 s."
    assert json.loads(json.dumps(chart)) == chart


def test_the_axis_runs_past_the_largest_bar_and_the_budget() -> None:
    over = hbar("Why", [Bar("Total, p95", 31.0)], x_label="Seconds", value_format=seconds, budget=Budget(25.0, "b"))
    under = hbar("Why", [Bar("Total, p95", 9.0)], x_label="Seconds", value_format=seconds, budget=Budget(25.0, "b"))
    assert over["ticks"][-1]["value"] >= 31.0 and under["ticks"][-1]["value"] >= 25.0
    assert over["ticks"][0]["value"] == under["ticks"][0]["value"] == 0


def test_ticks_can_be_worded_apart_from_the_values() -> None:
    chart = hbar("Why", [Bar("Total", 25.0)], x_label="Seconds", value_format=seconds, tick_format=lambda v: f"{v:g} s")
    assert [tick["label"] for tick in chart["ticks"]] == ["0 s", "10 s", "20 s", "30 s"]
    assert chart["bars"][0]["text"] == "25.00 s"


def test_a_label_from_the_data_reaches_the_browser_whole() -> None:
    # The browser fits a long label to its column and keeps the whole of it in the chart's table and description.
    reason = "Refused, " + "a very long refusal reason " * 4
    chart = hbar("Outcomes", [Bar(reason, 2, "bad")], x_label="Requests", value_format=str, integer=True)
    assert chart["bars"][0]["label"] == reason and reason in chart["description"]
    assert [tick["value"] for tick in chart["ticks"]] == [0, 1, 2]


def test_ticks_land_on_round_numbers_past_the_largest_value() -> None:
    assert nice_ticks(2.0) == [0, 0.5, 1.0, 1.5, 2.0]
    assert nice_ticks(25.0) == [0, 10, 20, 30]
    assert nice_ticks(3, integer=True) == [0, 1, 2, 3]
    assert nice_ticks(0, integer=True) == [0, 1]
