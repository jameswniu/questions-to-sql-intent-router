import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from html import escape

PAPER = "#ffffff"
PANEL = "#f6f8fa"
HAIRLINE = "#d0d7de"
INK = "#1f2328"
MUTED = "#57606a"
ACCENT = "#345c8f"
ACCENT_LIGHT = "#8ea7c7"
GOOD = "#1a7f37"
REFUSAL = "#a8433f"
FONT = "system-ui, -apple-system, Segoe UI, Helvetica, Arial, sans-serif"

# The dashboard is read at 100 percent zoom, so nothing is set smaller than this.
MIN_FONT = 14
# Estimated glyph width as a share of the font size, for a system sans-serif.
CHAR_WIDTH = 0.58
BOLD_CHAR_WIDTH = 0.62

WIDTH = 820
LABEL_W = 210
VALUE_W = 100
TITLE_H = 36
BUDGET_H = 26
ROW_H = 34
BAR_H = 20
AXIS_H = 56


class LabelOverflow(ValueError):
    pass


@dataclass(frozen=True)
class Bar:
    label: str
    value: float
    color: str = ACCENT


@dataclass(frozen=True)
class Budget:
    value: float
    label: str


def text_width(text: str, size: float, *, bold: bool = False) -> float:
    return len(text) * size * (BOLD_CHAR_WIDTH if bold else CHAR_WIDTH)


def check_fit(text: str, size: float, box: float, *, bold: bool = False) -> None:
    needed = text_width(text, size, bold=bold)
    if needed > box:
        raise LabelOverflow(f"{text!r} needs about {needed:.0f}px at {size}px, and its box is {box:.0f}px")


def clip(text: str, size: float, box: float, *, bold: bool = False) -> str:
    """Shortens a label that comes from the data until it fits. Labels written here only go through check_fit."""
    if text_width(text, size, bold=bold) <= box:
        return text
    keep = max(int(box / (size * (BOLD_CHAR_WIDTH if bold else CHAR_WIDTH))) - 1, 1)
    return text[:keep].rstrip() + "…"


def nice_ticks(top: float, *, integer: bool = False, target: int = 4) -> list[float]:
    top = top if top > 0 else 1.0
    raw = top / target
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in ((1, 2, 5, 10) if integer else (1, 2, 2.5, 5, 10)) if m * magnitude >= raw)
    if integer:
        step = max(1.0, float(round(step)))
    return [round(i * step, 10) for i in range(math.ceil(top / step - 1e-9) + 1)]


def _text(
    x: float,
    y: float,
    text: str,
    *,
    size: int,
    box: float,
    anchor: str = "start",
    bold: bool = False,
    fill: str = INK,
    halo: bool = False,
) -> str:
    if size < MIN_FONT:
        raise ValueError(f"{text!r} is set at {size}px, under the {MIN_FONT}px floor")
    check_fit(text, size, box, bold=bold)
    weight = ' font-weight="600"' if bold else ""
    # A paper-coloured outline drawn under the glyphs keeps a value readable where it crosses a gridline.
    outline = f' stroke="{PAPER}" stroke-width="4" paint-order="stroke"' if halo else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}"{weight}{outline}>'
        f"{escape(text)}</text>"
    )


def hbar(
    title: str,
    bars: Sequence[Bar],
    *,
    x_label: str,
    value_format: Callable[[float], str],
    tick_format: Callable[[float], str] | None = None,
    budget: Budget | None = None,
    integer: bool = False,
    width: int = WIDTH,
) -> str:
    """A horizontal bar chart as an SVG string. A budget is drawn as a dashed line with its label above it."""
    ticks = nice_ticks(max([bar.value for bar in bars] + [budget.value if budget else 0.0]), integer=integer)
    x0, x1 = LABEL_W, width - VALUE_W

    def at(value: float) -> float:
        return x0 + (x1 - x0) * value / ticks[-1]

    y0 = TITLE_H + (BUDGET_H if budget else 0)
    y_end = y0 + ROW_H * len(bars)
    spacing = (x1 - x0) / max(len(ticks) - 1, 1)
    parts = [_text(0, 22, title, size=16, box=width, bold=True)]
    for tick in ticks:
        x = at(tick)
        parts.append(f'<line x1="{x:.1f}" y1="{y0 - 4}" x2="{x:.1f}" y2="{y_end}" stroke="{HAIRLINE}"/>')
        label = (tick_format or value_format)(tick)
        parts.append(_text(x, y_end + 22, label, size=14, box=spacing, anchor="middle", fill=MUTED))
    for index, bar in enumerate(bars):
        middle = y0 + ROW_H * index + ROW_H / 2
        label = clip(bar.label, 15, x0 - 12)
        parts.append(_text(x0 - 12, middle + 5, label, size=15, box=x0 - 12, anchor="end"))
        end = at(bar.value)
        if bar.value > 0:
            parts.append(
                f'<rect x="{x0}" y="{middle - BAR_H / 2:.1f}" width="{max(end - x0, 1.0):.1f}" height="{BAR_H}" '
                f'rx="2" fill="{bar.color}"/>'
            )
        parts.append(_text(end + 8, middle + 5, value_format(bar.value), size=14, box=width - end - 8, halo=True))
    if budget is not None:
        x = at(budget.value)
        parts.append(
            f'<line x1="{x:.1f}" y1="{y0 - 8}" x2="{x:.1f}" y2="{y_end}" stroke="{INK}" stroke-width="1.5" '
            'stroke-dasharray="5 4"/>'
        )
        half = text_width(budget.label, 14) / 2
        centre = min(max(x, x0 + half), width - half)
        parts.append(_text(centre, y0 - 12, budget.label, size=14, box=width - x0, anchor="middle"))
    parts.append(_text((x0 + x1) / 2, y_end + 46, x_label, size=14, box=x1 - x0, anchor="middle", fill=MUTED))
    described = [f"{bar.label}: {value_format(bar.value)}" for bar in bars]
    if budget is not None:
        described.append(budget.label)
    return _svg(title, ". ".join(described) + ".", width, y_end + AXIS_H, parts)


def _svg(title: str, description: str, width: int, height: int, parts: list[str]) -> str:
    ident = "chart-" + hashlib.blake2s(f"{title}|{description}".encode(), digest_size=5).hexdigest()
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="chart" viewBox="0 0 {width} {height}" width="{width}" '
        f'height="{height}" role="img" aria-labelledby="{ident}-t {ident}-d" font-family="{FONT}">'
        f'<title id="{ident}-t">{escape(title)}</title><desc id="{ident}-d">{escape(description)}</desc>'
        + "".join(parts)
        + "</svg>"
    )
