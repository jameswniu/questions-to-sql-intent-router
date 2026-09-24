from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from app.sandbox.templates import TEMPLATES, decompose, slope, yoy, zscore


def _cents(x: float) -> int:
    return round(x * 100)


def test_yoy_reports_absolute_and_percent_change_with_groups_ranked_by_size() -> None:
    df = pd.DataFrame(
        {
            "yr": [2024, 2024, 2025, 2025, 2025],
            "peril": ["hail", "water", "hail", "water", "water"],
            "paid": [100.0, 300.0, 400.0, 250.0, 100.0],
        }
    )
    out = yoy(df, {"value": "paid", "period": "yr", "base": 2024, "current": 2025, "group": "peril"})
    assert (out["base"], out["current"], out["change"]) == (400.0, 750.0, 350.0)
    assert out["pct"] == pytest.approx(0.875)
    assert [g["group"] for g in out["groups"]] == ["hail", "water"]
    assert out["groups"][0]["pct"] == pytest.approx(3.0)
    assert sum(g["change"] for g in out["groups"]) == out["change"]


def test_yoy_percent_is_none_when_the_base_is_zero() -> None:
    df = pd.DataFrame({"yr": [2025], "paid": [10.0]})
    out = yoy(df, {"value": "paid", "period": "yr", "base": 2024, "current": 2025})
    assert out["change"] == 10.0 and out["pct"] is None


def test_decompose_matches_the_midpoint_formula_on_a_worked_example() -> None:
    # base: 2 claims, mean 150. current: 3 claims, mean 200. change 300.
    df = pd.DataFrame({"q": ["a", "a", "b", "b", "b"], "paid": [100.0, 200.0, 150.0, 150.0, 300.0]})
    out = decompose(df, {"value": "paid", "period": "q", "base": "a", "current": "b"})
    assert out["delta_total"] == 300.0
    assert out["count_effect"] == 175.0  # (3 - 2) * (150 + 200) / 2
    assert out["mean_effect"] == 125.0  # (200 - 150) * (2 + 3) / 2


def test_decompose_count_and_mean_effects_add_to_the_change_to_the_cent() -> None:
    rng = random.Random(20260924)
    for _ in range(300):
        rows = []
        for period in ("base", "current"):
            for _ in range(rng.randint(0, 40)):
                cents = rng.choice([rng.randint(1, 500_00), rng.randint(1, 250_000_00)])
                rows.append((period, rng.choice(["water", "wind", "hail", "fire"]), cents / 100))
        df = pd.DataFrame(rows, columns=["period", "peril", "paid"])
        out = decompose(
            df, {"value": "paid", "period": "period", "base": "base", "current": "current", "group": "peril"}
        )

        assert _cents(out["count_effect"]) + _cents(out["mean_effect"]) == _cents(out["delta_total"])
        for g in out["groups"]:
            assert _cents(g["count_effect"]) + _cents(g["mean_effect"]) == _cents(g["delta_total"])
        assert sum(_cents(g["delta_total"]) for g in out["groups"]) == _cents(out["delta_total"])


def test_decompose_ranks_groups_by_contribution_to_the_change() -> None:
    df = pd.DataFrame(
        {
            "yr": [2024, 2024, 2025, 2025, 2025],
            "peril": ["wind", "hail", "wind", "hail", "hail"],
            "paid": [100.0, 100.0, 90.0, 400.0, 500.0],
        }
    )
    out = decompose(df, {"value": "paid", "period": "yr", "base": 2024, "current": 2025, "group": "peril"})
    assert [g["group"] for g in out["groups"]] == ["hail", "wind"]
    assert out["groups"][0]["share"] == pytest.approx(800 / 790)


def test_decompose_gives_a_group_that_appears_only_now_entirely_to_count() -> None:
    df = pd.DataFrame({"yr": [2025, 2025], "peril": ["mold", "mold"], "paid": [120.0, 80.0]})
    out = decompose(df, {"value": "paid", "period": "yr", "base": 2024, "current": 2025})
    assert (out["delta_total"], out["count_effect"], out["mean_effect"]) == (200.0, 200.0, 0.0)


def test_decompose_accepts_pre_aggregated_counts() -> None:
    df = pd.DataFrame({"q": ["a", "b"], "claims": [2, 3], "paid": [300.0, 600.0]})
    out = decompose(df, {"value": "paid", "period": "q", "base": "a", "current": "b", "count": "claims"})
    assert (out["count_effect"], out["mean_effect"]) == (175.0, 125.0)


def test_zscore_flags_a_spike_against_the_trailing_window_only() -> None:
    values = [10, 12, 9, 11, 10, 12, 11, 30]
    df = pd.DataFrame({"month": [f"2025-{m:02d}" for m in range(1, 9)], "claims": values})
    out = zscore(df, {"value": "claims", "period": "month", "window": 6, "threshold": 2.0})
    rows = out["rows"]
    assert [r["z"] for r in rows[:3]] == [None, None, None]  # fewer than three periods of history
    window = [12, 9, 11, 10, 12, 11]  # the six months before the spike, not the first month
    assert rows[-1]["baseline_mean"] == pytest.approx(np.mean(window))
    assert rows[-1]["z"] == pytest.approx((30 - np.mean(window)) / np.std(window, ddof=1))
    assert out["flagged"] == ["2025-08"]


def test_zscore_is_none_when_the_baseline_has_no_spread() -> None:
    df = pd.DataFrame({"m": range(5), "claims": [7, 7, 7, 7, 9]})
    out = zscore(df, {"value": "claims", "period": "m"})
    assert out["rows"][-1]["z"] is None and out["flagged"] == []


def test_slope_recovers_an_exact_line_with_r2_of_one() -> None:
    df = pd.DataFrame({"q": ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"], "paid": [2.0, 5.0, 8.0, 11.0]})
    out = slope(df, {"value": "paid", "period": "q"})
    assert out["slope"] == pytest.approx(3.0) and out["intercept"] == pytest.approx(2.0)
    assert out["r2"] == pytest.approx(1.0)


def test_slope_agrees_with_numpy_polyfit_on_noisy_data() -> None:
    rng = np.random.default_rng(7)
    y = 50 + 4 * np.arange(12) + rng.normal(0, 6, 12)
    df = pd.DataFrame({"m": range(12), "paid": y})
    out = slope(df, {"value": "paid", "period": "m"})
    b, a = np.polyfit(np.arange(12), y, 1)
    assert out["slope"] == pytest.approx(b) and out["intercept"] == pytest.approx(a)
    assert 0 < out["r2"] < 1


def test_slope_needs_two_periods() -> None:
    out = slope(pd.DataFrame({"m": [1], "paid": [3.0]}), {"value": "paid", "period": "m"})
    assert out["slope"] is None


def test_every_template_has_a_one_line_docstring() -> None:
    for name, fn in TEMPLATES.items():
        assert fn.__doc__ and "\n" not in fn.__doc__.strip(), name
