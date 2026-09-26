import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, TypedDict

# What a bar's colour says. The browser maps each to its light or dark theme colour, and every bar is also labelled,
# so a colour is never the only way to tell two bars apart.
Tone = Literal["accent", "accent-light", "good", "bad", "neutral"]


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    tone: Tone = "accent"


@dataclass(frozen=True)
class Budget:
    value: float
    label: str


class Tick(TypedDict):
    value: float
    label: str


class BarView(TypedDict):
    label: str
    value: float
    # The value as the dashboard writes it, such as "1.60 s" or "$0.0042".
    text: str
    tone: Tone


class BudgetView(TypedDict):
    value: float
    label: str


class Chart(TypedDict):
    """A horizontal bar chart as the browser draws it. The numbers and their wording are settled here, so the chart,
    its table view and its description read the same."""

    title: str
    # Every bar and the budget read out, for anyone who can't see the chart.
    description: str
    x_label: str
    ticks: list[Tick]
    bars: list[BarView]
    budget: BudgetView | None


def nice_ticks(top: float, *, integer: bool = False, target: int = 4) -> list[float]:
    top = top if top > 0 else 1.0
    raw = top / target
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in ((1, 2, 5, 10) if integer else (1, 2, 2.5, 5, 10)) if m * magnitude >= raw)
    if integer:
        step = max(1.0, float(round(step)))
    return [round(i * step, 10) for i in range(math.ceil(top / step - 1e-9) + 1)]


def hbar(
    title: str,
    bars: Sequence[Bar],
    *,
    x_label: str,
    value_format: Callable[[float], str],
    tick_format: Callable[[float], str] | None = None,
    budget: Budget | None = None,
    integer: bool = False,
) -> Chart:
    """A horizontal bar chart whose axis runs from zero to a round number past the largest bar and the budget."""
    ticks = nice_ticks(max([bar.value for bar in bars] + [budget.value if budget else 0.0]), integer=integer)
    described = [f"{bar.label}: {value_format(bar.value)}" for bar in bars]
    if budget is not None:
        described.append(budget.label)
    return {
        "title": title,
        "description": ". ".join(described) + ".",
        "x_label": x_label,
        "ticks": [{"value": tick, "label": (tick_format or value_format)(tick)} for tick in ticks],
        "bars": [
            {"label": bar.label, "value": bar.value, "text": value_format(bar.value), "tone": bar.tone} for bar in bars
        ],
        "budget": None if budget is None else {"value": budget.value, "label": budget.label},
    }
