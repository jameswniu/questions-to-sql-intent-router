# The golden operations. This file is copied verbatim into the sandbox image, so it may import
# only pandas, numpy and the standard library, never anything from the app package.
from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction
from typing import Any

import numpy as np
import pandas as pd


def _pct(base: float, current: float) -> float | None:
    return None if base == 0 else (current - base) / abs(base)


def _cents(series: pd.Series) -> int:
    # Summing integer cents keeps the totals exact, so the decomposition identity holds to the cent.
    return int(sum(round(float(v) * 100) for v in series.dropna()))


def yoy(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Change in a value column between two periods, absolute and percent, per group when a group column is given."""
    value, period = params["value"], params["period"]
    base, current = params["base"], params["current"]
    group = params.get("group")

    def totals(frame: pd.DataFrame) -> tuple[float, float]:
        b = float(frame.loc[frame[period] == base, value].sum())
        c = float(frame.loc[frame[period] == current, value].sum())
        return b, c

    b, c = totals(df)
    out: dict[str, Any] = {"base": b, "current": c, "change": c - b, "pct": _pct(b, c)}
    if group:
        rows: list[dict[str, Any]] = []
        for key, frame in df.groupby(group, sort=True):
            gb, gc = totals(frame)
            rows.append({"group": str(key), "base": gb, "current": gc, "change": gc - gb, "pct": _pct(gb, gc)})
        rows.sort(key=lambda r: abs(r["change"]), reverse=True)
        out["groups"] = rows
    return out


def _split(n0: int, t0: int, n1: int, t1: int) -> tuple[int, int, int]:
    """Shapley split of t1 - t0 (cents) into count and mean effects, with total = n * mean."""
    delta = t1 - t0
    if n0 == 0 and n1 == 0:
        return delta, 0, 0
    # An absent side has no mean; borrowing the other side's mean assigns the whole change to count.
    m0 = Fraction(t0, n0) if n0 else Fraction(t1, n1)
    m1 = Fraction(t1, n1) if n1 else m0
    count_effect = round((n1 - n0) * (m0 + m1) / 2)
    return delta, count_effect, delta - count_effect


def decompose(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Change in a total split exactly into count and mean effects (midpoint Shapley), overall and per group."""
    value, period = params["value"], params["period"]
    base, current = params["base"], params["current"]
    count_col, group = params.get("count"), params.get("group")

    def side(frame: pd.DataFrame, p: Any) -> tuple[int, int]:
        rows = frame[frame[period] == p]
        n = int(rows[count_col].sum()) if count_col else len(rows)
        return n, _cents(rows[value])

    def effects(frame: pd.DataFrame) -> dict[str, Any]:
        n0, t0 = side(frame, base)
        n1, t1 = side(frame, current)
        delta, ce, me = _split(n0, t0, n1, t1)
        return {
            "n_base": n0,
            "n_current": n1,
            "total_base": t0 / 100,
            "total_current": t1 / 100,
            "mean_base": t0 / n0 / 100 if n0 else None,
            "mean_current": t1 / n1 / 100 if n1 else None,
            "delta_total": delta / 100,
            "count_effect": ce / 100,
            "mean_effect": me / 100,
        }

    out = effects(df)
    if group:
        rows = []
        for key, frame in df.groupby(group, sort=True):
            r = effects(frame)
            r["group"] = str(key)
            r["share"] = r["delta_total"] / out["delta_total"] if out["delta_total"] else None
            rows.append(r)
        rows.sort(key=lambda r: abs(r["delta_total"]), reverse=True)
        out["groups"] = rows
    return out


def zscore(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Z-score of each period's value against the trailing window before it, flagging |z| >= threshold."""
    value, period = params["value"], params["period"]
    window = int(params.get("window", 12))
    threshold = float(params.get("threshold", 2.0))
    min_periods = int(params.get("min_periods", 3))

    series = df.groupby(period, sort=True)[value].sum()
    values = series.to_numpy(dtype=float)
    rows = []
    for i, (p, x) in enumerate(zip(series.index, values, strict=True)):
        base = values[max(0, i - window) : i]
        mean = std = z = None
        if len(base) >= min_periods:
            mean, std = float(base.mean()), float(base.std(ddof=1))
            z = (float(x) - mean) / std if std > 0 else None
        rows.append(
            {
                "period": str(p),
                "value": float(x),
                "baseline_mean": mean,
                "baseline_std": std,
                "z": z,
                "flag": z is not None and abs(z) >= threshold,
            }
        )
    return {"threshold": threshold, "window": window, "rows": rows, "flagged": [r["period"] for r in rows if r["flag"]]}


def slope(df: pd.DataFrame, params: dict[str, Any]) -> dict[str, Any]:
    """Least-squares slope per period of a value over ordered periods, with intercept and r squared."""
    value, period = params["value"], params["period"]
    series = df.groupby(period, sort=True)[value].sum()
    y = series.to_numpy(dtype=float)
    n = len(y)
    if n < 2:
        return {"n": n, "slope": None, "intercept": None, "r2": None}
    x = np.arange(n, dtype=float)
    xd, yd = x - x.mean(), y - y.mean()
    b = float((xd * yd).sum() / (xd * xd).sum())
    a = float(y.mean() - b * x.mean())
    ss_tot = float((yd * yd).sum())
    ss_res = float(((y - (a + b * x)) ** 2).sum())
    r2 = None if ss_tot == 0 else 1.0 - ss_res / ss_tot
    return {
        "n": n,
        "slope": b,
        "intercept": a,
        "r2": r2,
        "first_period": str(series.index[0]),
        "last_period": str(series.index[-1]),
    }


TEMPLATES: dict[str, Callable[[pd.DataFrame, dict[str, Any]], dict[str, Any]]] = {
    "yoy": yoy,
    "decompose": decompose,
    "zscore": zscore,
    "slope": slope,
}
