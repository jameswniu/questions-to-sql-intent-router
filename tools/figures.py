"""Draws docs/figures/system-map.svg. Run `uv run python tools/figures.py`, and add --check to fail when the file on
disk is stale.

Every label is measured before it is drawn. GitHub shows a README image in a column about 837 px wide, so a
1200-unit figure is scaled by about 0.7 and a 23-unit font lands near 16 px, which is still readable when the
page is viewed at 75%.

The map is a grid: every box is the same size, the five columns are evenly spaced, and every connector runs
straight across or straight down. One accent marks the path a question takes to its answer, and the rest is gray.
"""

from __future__ import annotations

import html
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "figures" / "system-map.svg"

SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans',Helvetica,Arial,sans-serif"
MIN_FONT = 23
LABEL, NOTE = 24, 23
PAD = 14
LEADING = 30

INK, MUTED, CANVAS = "#1f2328", "#59636e", "#ffffff"
ACCENT, ACCENT_FILL = "#0969da", "#ddf4ff"
GRAY, GRAY_FILL, GRAY_EDGE = "#8c959f", "#ffffff", "#afb8c1"
GROUP_FILL, GROUP_EDGE = "#f6f8fa", "#d1d9e0"

WIDTH, MARGIN = 1200, 24
BOX_W, BOX_H = 168, 56
# The group around the four paths: padding at its sides, on top where its name sits, and under the last path, then
# the gap between two paths.
INSET, HEAD, FOOT, STACK = 24, 44, 20, 12
# From the group down to the database row, which leaves room for the one line about whose login a query runs as.
DROP = 100


def width(text: str, size: int, bold: bool = False) -> float:
    """A deliberately generous estimate for a proportional sans face."""
    return len(text) * size * (0.62 if bold else 0.58)


def text(x: float, y: float, s: str, size: int, fill: str, bold: bool = False, anchor: str = "middle") -> str:
    if size < MIN_FONT:
        raise SystemExit(f"{s!r} is set at {size}, under the {MIN_FONT}-unit floor")
    weight = ' font-weight="600"' if bold else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" font-family="{SANS}" font-size="{size}"{weight} '
        f'fill="{fill}">{html.escape(s)}</text>'
    )


@dataclass(frozen=True)
class Box:
    x: float
    y: float
    label: str
    accent: bool = False

    @property
    def cx(self) -> float:
        return self.x + BOX_W / 2

    @property
    def cy(self) -> float:
        return self.y + BOX_H / 2

    @property
    def right(self) -> float:
        return self.x + BOX_W

    @property
    def bottom(self) -> float:
        return self.y + BOX_H

    def svg(self) -> list[str]:
        if len(self.label.split()) > 2:
            raise SystemExit(f"{self.label!r} is more than two words")
        need = width(self.label, LABEL, bold=True)
        if need > BOX_W - 2 * PAD:
            raise SystemExit(f"{self.label!r} needs {need:.0f} units in a {BOX_W}-unit box")
        fill, edge = (ACCENT_FILL, ACCENT) if self.accent else (GRAY_FILL, GRAY_EDGE)
        return [
            f'<rect x="{self.x:.0f}" y="{self.y:.0f}" width="{BOX_W}" height="{BOX_H}" rx="10" fill="{fill}" '
            f'stroke="{edge}" stroke-width="2"/>',
            text(self.cx, self.cy + LABEL * 0.35, self.label, LABEL, INK, bold=True),
        ]


def arrow(start: tuple[float, float], end: tuple[float, float], accent: bool = False) -> str:
    """A connector straight across or straight down, with a small head where it meets a box."""
    (x1, y1), (x2, y2) = start, end
    if x1 != x2 and y1 != y2:
        raise SystemExit(f"the connector from {start} to {end} is neither across nor down")
    color, head = (ACCENT, "head") if accent else (GRAY, "head-gray")
    return (
        f'<path d="M{x1:.0f},{y1:.0f} L{x2:.0f},{y2:.0f}" fill="none" stroke="{color}" stroke-width="2" '
        f'marker-end="url(#{head})"/>'
    )


def note(x: float, middle: float, lines: tuple[str, ...], right: float) -> list[str]:
    """Lines set from x and centered on middle, each of which must end before right."""
    first = middle - LEADING * (len(lines) - 1) / 2 + NOTE * 0.35
    out = []
    for i, line in enumerate(lines):
        need = width(line, NOTE)
        if x + need > right:
            raise SystemExit(f"{line!r} needs {need:.0f} units and has {right - x:.0f}")
        out.append(text(x, first + i * LEADING, line, NOTE, MUTED, anchor="start"))
    return out


def marker(name: str, color: str) -> str:
    return (
        f'<marker id="{name}" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" markerWidth="10" '
        f'markerHeight="10" orient="auto"><path d="M0,1 L10,5 L0,9 z" fill="{color}"/></marker>'
    )


def system_map() -> str:
    gap = (WIDTH - 2 * MARGIN - 5 * BOX_W - 2 * INSET) / 4
    group_x, group_y = MARGIN + 2 * (BOX_W + gap), MARGIN
    group_w, group_h = BOX_W + 2 * INSET, HEAD + 4 * BOX_H + 3 * STACK + FOOT
    paths = [
        Box(group_x + INSET, group_y + HEAD + i * (BOX_H + STACK), label, accent=True)
        for i, label in enumerate(("Lookup", "Figures", "Documents", "Why"))
    ]
    mid = (paths[0].y + paths[-1].bottom) / 2
    row = mid - BOX_H / 2
    gate = Box(MARGIN, row, "Gate", accent=True)
    router = Box(gate.right + gap, row, "Router", accent=True)
    verifier = Box(group_x + group_w + gap, row, "Verifier", accent=True)
    answer = Box(verifier.right + gap, row, "Answer", accent=True)
    # The why path is the only one that runs analysis code, so the sandbox sits level with it.
    sandbox = Box(verifier.x, paths[-1].y, "Sandbox")
    base = group_y + group_h + DROP
    postgres = Box(paths[0].x, base, "Postgres")
    ingest = Box(router.x, base, "Ingest")
    if answer.right != WIDTH - MARGIN:
        raise SystemExit(f"the columns end at {answer.right:.0f}, not {WIDTH - MARGIN}")

    name = "Paths"
    if width(name, NOTE, bold=True) > group_w - 2 * 18:
        raise SystemExit(f"{name!r} doesn't fit its group")
    height = postgres.bottom + MARGIN
    parts = [
        f'<rect width="{WIDTH}" height="{height:.0f}" fill="{CANVAS}"/>',
        f'<rect x="{group_x:.0f}" y="{group_y:.0f}" width="{group_w:.0f}" height="{group_h:.0f}" rx="14" '
        f'fill="{GROUP_FILL}" stroke="{GROUP_EDGE}" stroke-width="1.5"/>',
        text(group_x + 18, group_y + 31, name, NOTE, MUTED, bold=True, anchor="start"),
        arrow((gate.right, mid), (router.x, mid), accent=True),
        arrow((router.right, mid), (group_x, mid), accent=True),
        arrow((group_x + group_w, mid), (verifier.x, mid), accent=True),
        arrow((verifier.right, mid), (answer.x, mid), accent=True),
        arrow((paths[-1].right, paths[-1].cy), (sandbox.x, sandbox.cy)),
        arrow((postgres.cx, group_y + group_h), (postgres.cx, postgres.y)),
        arrow((ingest.right, ingest.cy), (postgres.x, postgres.cy)),
        *[part for box in [gate, router, *paths, verifier, answer, sandbox, postgres, ingest] for part in box.svg()],
        # The permission model, said once: whatever path a question takes, the database answers the asker's login.
        *note(
            postgres.cx + 20,
            (group_y + group_h + postgres.y) / 2,
            ("Every query runs as the asker's own login",),
            WIDTH - MARGIN,
        ),
    ]
    defs = f"<defs>{marker('head', ACCENT)}{marker('head-gray', GRAY)}</defs>"
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height:.0f}" width="{WIDTH}" '
            f'height="{height:.0f}" role="img" aria-label="A question passes the gate and the router, then the lookup, '
            "figures, documents or why path. Every path queries Postgres as the asker's own login, and the why path "
            "also uses a sandbox. The verifier checks the draft before the answer. Ingest loads documents and scans "
            'into Postgres.">',
            defs,
            *parts,
            "</svg>",
            "",
        ]
    )


def main() -> None:
    svg = system_map()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != svg:
            raise SystemExit(f"{OUT.relative_to(ROOT)} is stale; run uv run python tools/figures.py")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
