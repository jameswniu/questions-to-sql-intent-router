import re

import pytest

from app.web.charts import MIN_FONT, Bar, Budget, LabelOverflow, check_fit, hbar, nice_ticks


def seconds(value: float) -> str:
    return f"{value:.2f} s"


def test_fit_guard_raises_on_a_label_wider_than_its_box() -> None:
    check_fit("Lookup", 16, 100)
    with pytest.raises(LabelOverflow):
        check_fit("Average paid per closed claim", 16, 100)
    # Bold runs wider: 16 characters fit at 14px regular in 130px, not in bold.
    check_fit("Total latency p9", 14, 130)
    with pytest.raises(LabelOverflow):
        check_fit("Total latency p9", 14, 130, bold=True)


def test_a_chart_whose_own_label_overflows_refuses_to_render() -> None:
    with pytest.raises(LabelOverflow):
        hbar("Requests", [Bar("Lookup", 3)], x_label="Requests " * 20, value_format=str)


def test_labels_from_the_data_are_clipped_to_their_column() -> None:
    reason = "Refused, " + "a very long refusal reason " * 4
    svg = hbar("Outcomes", [Bar(reason, 2)], x_label="Requests", value_format=str, integer=True)
    assert "…" in svg
    assert f">{reason}</text>" not in svg
    assert "<desc id=" in svg and reason in svg  # the full label stays in the description


def test_a_chart_has_a_title_an_axis_label_the_budget_line_and_no_small_text() -> None:
    svg = hbar(
        "Lookup, 12 requests",
        [Bar("Total, p50", 0.4), Bar("Total, p95", 1.3)],
        x_label="Seconds",
        value_format=seconds,
        budget=Budget(2.0, "p95 budget 2 s"),
    )
    assert "<title" in svg and ">Lookup, 12 requests</text>" in svg
    assert ">Seconds</text>" in svg and ">p95 budget 2 s</text>" in svg
    assert 'stroke-dasharray="5 4"' in svg
    assert min(int(size) for size in re.findall(r'font-size="(\d+)"', svg)) >= MIN_FONT


def test_ticks_land_on_round_numbers_past_the_largest_value() -> None:
    assert nice_ticks(2.0) == [0, 0.5, 1.0, 1.5, 2.0]
    assert nice_ticks(25.0) == [0, 10, 20, 30]
    assert nice_ticks(3, integer=True) == [0, 1, 2, 3]
    assert nice_ticks(0, integer=True) == [0, 1]
