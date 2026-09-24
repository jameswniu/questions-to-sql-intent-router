"""Draws docs/figures/system-map.svg. Run `make figures`; the check mode fails if the file on disk is stale.

Every label is measured before it is drawn. GitHub shows a README image in a column about 837 px wide, so a
1200-unit figure is scaled by about 0.7 and a 23-unit font lands near 16 px, which is still readable when the
page is viewed at 75%.
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
BODY, TITLE = 23, 26
PAD = 14

INK, MUTED, EDGE = "#1f2328", "#59636e", "#d1d9e0"
CANVAS, SUBTLE = "#ffffff", "#f6f8fa"
BLUE_BG, BLUE_EDGE = "#ddf4ff", "#54aeff"
GREEN_BG, GREEN_EDGE = "#dafbe1", "#4ac26b"


def width(text: str, size: int, bold: bool = False) -> float:
    """A deliberately generous estimate for a proportional sans face."""
    return len(text) * size * (0.62 if bold else 0.58)


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int
    lines: tuple[str, ...]
    fill: str = SUBTLE
    edge: str = EDGE

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2

    def svg(self) -> list[str]:
        out = [
            f'<rect x="{self.x}" y="{self.y}" width="{self.w}" height="{self.h}" rx="10" '
            f'fill="{self.fill}" stroke="{self.edge}" stroke-width="2"/>'
        ]
        sizes = [TITLE] + [BODY] * (len(self.lines) - 1)
        step = 32
        top = self.cy - step * (len(self.lines) - 1) / 2 + 8
        for i, (line, size) in enumerate(zip(self.lines, sizes, strict=True)):
            bold = i == 0
            need = width(line, size, bold)
            if need > self.w - 2 * PAD:
                raise SystemExit(f"{line!r} needs {need:.0f} units in a {self.w}-unit box")
            out.append(text(self.cx, top + i * step, line, size, INK if bold else MUTED, bold))
        return out


def text(x: float, y: float, s: str, size: int, fill: str, bold: bool = False, anchor: str = "middle") -> str:
    if size < MIN_FONT:
        raise SystemExit(f"{s!r} is set at {size}, under the {MIN_FONT}-unit floor")
    weight = ' font-weight="600"' if bold else ""
    return (
        f'<text x="{x:.0f}" y="{y:.0f}" text-anchor="{anchor}" font-family="{SANS}" font-size="{size}"{weight} '
        f'fill="{fill}">{html.escape(s)}</text>'
    )


def path(points: list[tuple[float, float]], dashed: bool = False, arrow: bool = True) -> str:
    d = " ".join(f"{'M' if i == 0 else 'L'}{x:.0f},{y:.0f}" for i, (x, y) in enumerate(points))
    dash = ' stroke-dasharray="7 6"' if dashed else ""
    head = ' marker-end="url(#head)"' if arrow else ""
    return f'<path d="{d}" fill="none" stroke="{MUTED}" stroke-width="2"{dash}{head}/>'


def system_map() -> str:
    lanes_x, lanes_w, lane_h, lane_gap, top = 410, 250, 80, 14, 30
    lanes = [
        Box(lanes_x, top + i * (lane_h + lane_gap), lanes_w, lane_h, lines, BLUE_BG, BLUE_EDGE)
        for i, lines in enumerate(
            [
                ("Lookup", "one claim by id"),
                ("Figures", "semantic layer"),
                ("Documents", "hybrid search"),
                ("Why", "drivers + memos"),
            ]
        )
    ]
    mid = (lanes[0].cy + lanes[-1].cy) / 2
    gate = Box(30, int(mid - 40), 150, 80, ("Gate",))
    router = Box(220, int(mid - 40), 150, 80, ("Router",))
    verifier = Box(700, int(mid - 55), 230, 110, ("Verifier", "numbers and", "citations"), GREEN_BG, GREEN_EDGE)
    answer = Box(966, int(mid - 40), 210, 80, ("Answer", "with evidence"))
    lanes_bottom = lanes[-1].y + lane_h
    row = lanes_bottom + 58
    ingest = Box(40, row, 310, 110, ("Ingest", "docs, notes, scans", "mask, screen, OCR"))
    postgres = Box(395, row, 290, 110, ("Postgres", "claims and chunks", "row-level security"))
    sandbox = Box(730, row, 260, 80, ("Sandbox", "no network, 10 s"))

    parts = [
        box_svg for box in [gate, router, *lanes, verifier, answer, ingest, postgres, sandbox] for box_svg in box.svg()
    ]
    parts += [
        path([(gate.x + gate.w, mid), (router.x - 2, mid)]),
        path([(router.x + router.w, mid), (390, mid)], arrow=False),
        path([(390, lanes[0].cy), (390, lanes[-1].cy)], arrow=False),
        *[path([(390, lane.cy), (lanes_x - 2, lane.cy)]) for lane in lanes],
        *[path([(lane.x + lane.w, lane.cy), (680, lane.cy)], arrow=False) for lane in lanes],
        path([(680, lanes[0].cy), (680, lanes[-1].cy)], arrow=False),
        path([(680, mid), (verifier.x - 2, mid)]),
        path([(verifier.x + verifier.w, mid), (answer.x - 2, mid)]),
        path([(ingest.x + ingest.w, ingest.cy), (postgres.x - 2, ingest.cy)]),
        path([(postgres.cx, lanes_bottom), (postgres.cx, postgres.y - 2)], dashed=True),
        path(
            [
                (lanes[-1].x + lanes[-1].w - 30, lanes_bottom),
                (lanes[-1].x + lanes[-1].w - 30, row - 26),
                (sandbox.cx, row - 26),
                (sandbox.cx, sandbox.y - 2),
            ],
            dashed=True,
        ),
        text(gate.cx, gate.y + gate.h + 34, "turns away", BODY, MUTED),
        text(gate.cx, gate.y + gate.h + 62, "off-topic", BODY, MUTED),
        text(router.cx, router.y + router.h + 34, "rules first,", BODY, MUTED),
        text(router.cx, router.y + router.h + 62, "or asks back", BODY, MUTED),
        text(postgres.cx - 14, lanes_bottom + 36, "as the asker's own login", BODY, MUTED, anchor="end"),
    ]
    height = row + 110 + 30
    defs = (
        '<defs><marker id="head" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z" fill="{MUTED}"/></marker></defs>'
    )
    return "\n".join(
        [
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 {height}" width="1200" height="{height}" '
            'role="img" aria-label="A question passes the gate and the router, takes one of four paths that read '
            "Postgres as the asker, and is checked by the verifier before the answer is shown. Documents and scans "
            'are ingested into the same database.">',
            defs,
            f'<rect width="1200" height="{height}" fill="{CANVAS}"/>',
            *parts,
            "</svg>",
            "",
        ]
    )


def main() -> None:
    svg = system_map()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != svg:
            raise SystemExit(f"{OUT.relative_to(ROOT)} is stale; run make figures")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg)
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
