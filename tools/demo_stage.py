"""The stage the demo clips play on, and the moves drawn over them.

The recorder captures the app's own pixels and logs what it did: each caption, what the spotlight is on, every
glide and press of the pointer, and the typing. This module holds what both halves of a recording
share: the geometry and timing of the stage, how those logs turn into a position or an opacity at any moment, and
the HTML the recorder's Chromium draws the chrome from (the stage, the window frame, the caption lines, the title
card and the pointer). demo_render.py composites them on the host at exact frame times, so every glide is smooth
whatever rate the page was captured at.

It imports nothing outside the standard library, because it also runs in the recording container.
"""

import html
import math
import random
import zlib
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

Rect = tuple[float, float, float, float]  # left, top, right, bottom, in CSS pixels of the page
Point = tuple[float, float]

# The page, in CSS pixels, and the density it is captured at.
VIEW_W, VIEW_H = 900, 636
CAPTURE_SCALE = 2

# The mp4: a 1080p stage with the app's window in the middle and one caption line under it. The window scales the
# page by 4/3, so the app's 18 px text lands at 24 px.
FPS = 30
STAGE_W, STAGE_H = 1920, 1080
CONTENT_SCALE = 4 / 3
CONTENT_W, CONTENT_H = round(VIEW_W * CONTENT_SCALE), round(VIEW_H * CONTENT_SCALE)
BAR_H = 44
WINDOW_RADIUS = 14
CAPTION_GAP = 26
CAPTION_H = 40
WINDOW_X = (STAGE_W - CONTENT_W) // 2
WINDOW_Y = (STAGE_H - (BAR_H + CONTENT_H + CAPTION_GAP + CAPTION_H)) // 2
CONTENT_X, CONTENT_Y = WINDOW_X, WINDOW_Y + BAR_H
CAPTION_Y = CONTENT_Y + CONTENT_H + CAPTION_GAP
ADDRESS = "localhost:8000"

# The README's GIF: the window full width, a slim browser bar on top and the caption in a footer under it.
GIF_BAR_H = 34
GIF_FOOTER_H = 52
GIF_W, GIF_H = VIEW_W, GIF_BAR_H + VIEW_H + GIF_FOOTER_H

# Timing, in seconds. The title card and the fade back to the stage frame the mp4 only.
TITLE_S = 1.5  # the whole card: it fades in, holds, then crossfades into the window
TITLE_FADE_S = 0.3
WINDOW_FADE_S = 0.3
OUTRO_S = 0.5  # the window fades back to the empty stage, so a loop restarts on the stage it began on
CAPTION_FADE_S = 0.2
SPOT_GLIDE_S = 0.3
SPOT_FADE_S = 0.25
CURSOR_FADE_S = 0.2
CURSOR_IDLE_S = 1.2  # a pointer left alone this long fades out, as it does once a reading pause starts
RIPPLE_S = 0.5
GLIDE_MIN_S, GLIDE_MAX_S = 0.5, 0.7
GLIDE_FAR_PX = 700  # a glide this long or longer takes GLIDE_MAX_S
GLIDE_ARC = 0.08  # how far the path bows from a straight line, as a share of its length
GLIDE_STEPS_PER_S = 40  # the page's own pointer moves this often along the path, so every hover shows

# The spotlight dims the rest of the page by SPOT_DIM towards SPOT_TINT, through a rounded cutout.
SPOT_DIM = 0.5
SPOT_TINT = (21, 24, 29)  # the app's own text colour
SPOT_RADIUS = 12.0
SPOT_GROW = 14.0  # the cutout closes in by this much as it fades in
SPOT_PAD = 6.0  # the cutout's room around what it shows
# Outside the cutout, what lies just above the page's floor (the composer's top, or the window's bottom edge) fades
# into the page's background over this many CSS pixels, so a line cut off there dissolves instead of peeking out.
FLOOR_FADE_PX = 40.0
CANVAS = (246, 247, 249)  # the app's --canvas

# Typing: each key's pause is jittered between these, with a longer beat before a word or after punctuation.
TYPE_MIN_MS, TYPE_MAX_MS = 45.0, 110.0
TYPE_SPACE_MS = 60.0
TYPE_PUNCT_MS = 140.0
PUNCTUATION = ",.;:?!"

# The pointer, as the recorder's Chromium draws it: at CURSOR_SPRITE_SCALE times its size, with its tip here.
CURSOR_SPRITE_SCALE = 4
CURSOR_TIP = (3.0, 3.0)  # in CSS pixels of the sprite's own box, before scaling
RIPPLE_COLOR = (59, 91, 219)  # the app's accent, for the ring; its fill is white, so it shows on an accent button
RIPPLE_RADIUS = (6.0, 26.0)


def ease(t: float) -> float:
    """Cubic ease in and out over 0 to 1, clamped."""
    t = min(1.0, max(0.0, t))
    return 4 * t**3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out(t: float) -> float:
    t = min(1.0, max(0.0, t))
    return 1 - (1 - t) ** 3


def lerp(a: float, b: float, k: float) -> float:
    return a + (b - a) * k


def lerp_rect(a: Rect, b: Rect, k: float) -> Rect:
    return (lerp(a[0], b[0], k), lerp(a[1], b[1], k), lerp(a[2], b[2], k), lerp(a[3], b[3], k))


def grow(rect: Rect, by: float) -> Rect:
    return (rect[0] - by, rect[1] - by, rect[2] + by, rect[3] + by)


def typing_delays(text: str, mean_ms: float) -> list[float]:
    """The pause before each key of text, in ms. Each is jittered between TYPE_MIN_MS and TYPE_MAX_MS, a key that
    starts a word or follows punctuation waits longer, and the pauses add up to what len(text) keys at mean_ms take,
    so a question types in about the time it always did. The first key has no pause. The same text always types the
    same way, so a take can be recorded again."""
    if len(text) < 2:
        return [0.0] * len(text)
    rng = random.Random(zlib.crc32(text.encode()))
    pairs = list(zip(text, text[1:], strict=False))
    extra = [TYPE_SPACE_MS if before == " " else TYPE_PUNCT_MS if before in PUNCTUATION else 0.0 for before, _ in pairs]
    spread = TYPE_MAX_MS - TYPE_MIN_MS
    # What the jitter above the floor must add up to, and a skew that makes its mean land near there.
    room = mean_ms * len(text) - sum(extra) - TYPE_MIN_MS * len(pairs)
    power = max(0.3, spread / max(1.0, room / len(pairs)) - 1)
    jitter = [spread * rng.random() ** power for _ in pairs]
    scale = room / sum(jitter) if room > 0 and sum(jitter) > 0 else 0.0
    return [
        0.0,
        *(min(TYPE_MAX_MS, TYPE_MIN_MS + value * scale) + bump for value, bump in zip(jitter, extra, strict=True)),
    ]


def caption_at(changes: Sequence[tuple[float, str]], t: float) -> tuple[str | None, str | None, float]:
    """The caption line at t, as the caption fading out, the caption fading in and how far that crossfade has got.
    The first caption is there from the start, since it arrives with the window."""
    if not changes:
        return None, None, 1.0
    index = bisect_right([at for at, _ in changes], t) - 1
    if index <= 0:
        return changes[0][1], changes[0][1], 1.0
    at, text = changes[index]
    return changes[index - 1][1], text, ease((t - at) / CAPTION_FADE_S)


def glide_seconds(distance: float) -> float:
    """How long the pointer takes over a distance in CSS pixels: GLIDE_MIN_S for a short hop, up to GLIDE_MAX_S."""
    return GLIDE_MIN_S + (GLIDE_MAX_S - GLIDE_MIN_S) * min(1.0, distance / GLIDE_FAR_PX)


def glide_point(start: Point, end: Point, progress: float) -> Point:
    """Where the pointer is after progress (0 to 1) of the time a glide takes: eased in and out, on a path that bows
    a little to one side, the way a hand moves a mouse."""
    s = ease(progress)
    (x0, y0), (x1, y1) = start, end
    # The control point sits off the midpoint, square to the path.
    cx = (x0 + x1) / 2 - (y1 - y0) * GLIDE_ARC
    cy = (y0 + y1) / 2 + (x1 - x0) * GLIDE_ARC
    return (
        (1 - s) ** 2 * x0 + 2 * (1 - s) * s * cx + s**2 * x1,
        (1 - s) ** 2 * y0 + 2 * (1 - s) * s * cy + s**2 * y1,
    )


def pointer_start() -> Point:
    """Where the pointer first appears in a clip, before its first glide: right of centre, clear of the top bar."""
    return (VIEW_W * 0.64, VIEW_H * 0.52)


@dataclass(frozen=True)
class Glide:
    start: float
    end: float
    origin: Point
    target: Point


@dataclass(frozen=True)
class Press:
    at: float
    point: Point


class CursorTrack:
    """The drawn pointer from the recorder's log: where it is and how visible. It fades in as a glide starts, hides
    while a question is typed and once a reading pause starts, and fades out when left alone for CURSOR_IDLE_S."""

    def __init__(self, events: Sequence[Sequence[Any]]) -> None:
        self.glides: list[Glide] = []
        self.presses: list[Press] = []
        typing: list[tuple[float, float]] = []  # anything the pointer hides for: typing, or a reading pause
        for event in events:
            kind, values = event[0], [float(value) for value in event[1:]]
            if kind == "glide":
                self.glides.append(Glide(values[0], values[1], (values[2], values[3]), (values[4], values[5])))
            elif kind == "press":
                self.presses.append(Press(values[0], (values[1], values[2])))
            elif kind == "type":
                typing.append((values[0], values[1]))
            elif kind == "rest":
                typing.append((values[0], values[0]))
        self.glides.sort(key=lambda glide: glide.start)
        self.presses.sort(key=lambda press: press.at)
        self.spans = self._spans(sorted(typing))

    def _spans(self, typing: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """The stretches the pointer shows for, as (from, until) before the fade out."""
        moves = sorted(
            [(glide.start, glide.end, "glide") for glide in self.glides] + [(p.at, p.at, "press") for p in self.presses]
        )
        spans: list[tuple[float, float]] = []
        start: float | None = None
        last = 0.0
        for began, ended, kind in moves:
            cut = next((at for at, _ in typing if start is not None and last <= at <= began), None)
            if start is not None and (cut is not None or began > last + CURSOR_IDLE_S):
                spans.append((start, cut if cut is not None else last + CURSOR_IDLE_S))
                start = None
            if start is None:
                if kind != "glide":
                    continue  # a press with no glide before it would pop up from nowhere
                start = began
            last = max(last, ended)
        if start is not None:
            cut = next((at for at, _ in typing if last <= at), None)
            spans.append((start, cut if cut is not None and cut < last + CURSOR_IDLE_S else last + CURSOR_IDLE_S))
        return spans

    def point(self, t: float) -> Point:
        index = bisect_right([glide.start for glide in self.glides], t) - 1
        if index < 0:
            return self.glides[0].origin if self.glides else pointer_start()
        glide = self.glides[index]
        if t >= glide.end:
            return glide.target
        return glide_point(glide.origin, glide.target, (t - glide.start) / (glide.end - glide.start))

    def opacity(self, t: float) -> float:
        """0 to 1. One stretch can still be fading out as the next fades in, and the stronger of the two shows."""
        found = 0.0
        for start, until in self.spans:
            if start <= t < until + CURSOR_FADE_S:
                shown = ease((t - start) / CURSOR_FADE_S)
                found = max(found, min(shown, 1 - ease((t - until) / CURSOR_FADE_S)) if t > until else shown)
        return found

    def ripples(self, t: float) -> list[tuple[Point, float]]:
        """Each click still rippling at t, with how far its ripple has got, 0 to 1."""
        return [(press.point, (t - press.at) / RIPPLE_S) for press in self.presses if 0 <= t - press.at < RIPPLE_S]


@dataclass(frozen=True)
class SpotSegment:
    at: float
    origin: Rect
    target: Rect
    glide_s: float
    opacity_from: float
    opacity_to: float
    floor: float | None = None  # where the page's content stops, in CSS pixels from its top


class SpotlightTrack:
    """The spotlight from the page's log of what it shows: "focus" glides the cutout to a new rect, or grows it in
    there when the spotlight was off; "track" follows a focused rect that moved; "clear" fades the dim out. Each
    entry can also carry the page's floor, which the page reports with it."""

    def __init__(self, entries: Sequence[tuple[float, str, Rect | None, float | None]]) -> None:
        self.segments: list[SpotSegment] = []
        self.floors: list[tuple[float, float]] = []
        for at, kind, rect, floor in sorted(entries, key=lambda entry: entry[0]):
            if floor is not None:
                self.floors.append((at, floor))
            shown, opacity = self.at(at)
            if kind == "clear" or rect is None:
                if shown is not None and opacity > 0:
                    self.segments.append(SpotSegment(at, shown, shown, 0.0, opacity, 0.0))
                continue
            if kind == "track":
                # The focused rect moved on the page: follow it at once, or finish a glide on time at its new place.
                if shown is None:
                    continue
                last = self.segments[-1]
                left = max(0.0, last.at + last.glide_s - at)
                self.segments.append(SpotSegment(at, shown, rect, left, opacity, last.opacity_to))
            elif shown is None or opacity < 0.05:
                # Nothing to glide from: the cutout closes in on the rect as the dim fades in.
                self.segments.append(SpotSegment(at, grow(rect, SPOT_GROW), rect, SPOT_FADE_S, opacity, 1.0))
            else:
                self.segments.append(SpotSegment(at, shown, rect, SPOT_GLIDE_S, opacity, 1.0))

    def floor_at(self, t: float) -> float | None:
        """The page's floor as last reported at or before t, or the first one reported."""
        if not self.floors:
            return None
        index = bisect_right([at for at, _ in self.floors], t) - 1
        return self.floors[max(0, index)][1]

    def at(self, t: float) -> tuple[Rect | None, float]:
        """The cutout's rect and the dim's opacity (0 to 1) at t."""
        index = bisect_right([segment.at for segment in self.segments], t) - 1
        if index < 0:
            return None, 0.0
        segment = self.segments[index]
        k = 1.0 if segment.glide_s <= 0 else ease((t - segment.at) / segment.glide_s)
        opacity = lerp(segment.opacity_from, segment.opacity_to, ease((t - segment.at) / SPOT_FADE_S))
        return lerp_rect(segment.origin, segment.target, k), opacity


def frame_at(times: Sequence[float], t: float) -> int:
    """The captured frame on screen at t: the last one taken at or before it, or the first when none was."""
    return max(0, bisect_right(times, t) - 1)


def distance(a: Point, b: Point) -> float:
    return math.dist(a, b)


# The chrome, drawn by the recorder's Chromium as HTML so its type matches the app's. The font stack is the app's own.
FONT = 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif'
# The app's brand mark, the chat bubble on its accent square, from .brand::before in app/web/static/style.css.
BRAND_PATHS = (
    "M3 3h10a1 1 0 0 1 1 1v6.2a1 1 0 0 1-1 1H8.2L5 13.8v-2.6H3a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z",
    "M5 6h6M5 8.4h3.6",
)
ACCENT = "#3b5bdb"
# Liberation Sans, the app's face in the recorder's Chromium, has no medium weight, so a hairline stroke stands in.
MEDIUM = "font-weight: 500; -webkit-text-stroke: 0.3px currentColor;"
CAPTION_ROOM_PX = 24  # the least room between a caption and the mode label beside it

BASE_CSS = f"""
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; }}
body {{ position: relative; overflow: hidden; font-family: {FONT}; -webkit-font-smoothing: antialiased; }}
.dots {{ display: flex; }}
.dots i {{ display: block; border-radius: 50%; background: #d9dce2; }}
.address {{
  position: absolute; left: 50%; transform: translateX(-50%); display: flex; align-items: center;
  justify-content: center; background: #eff1f4; color: #5d6574; letter-spacing: 0.01em;
}}
"""
STAGE_CSS = f"""
body {{ width: {STAGE_W}px; height: {STAGE_H}px; background: linear-gradient(165deg, #eef1f6, #f6f7f9); }}
.window {{
  position: absolute; left: {WINDOW_X}px; top: {WINDOW_Y}px; width: {CONTENT_W}px; height: {BAR_H + CONTENT_H}px;
  border-radius: {WINDOW_RADIUS}px; overflow: hidden; background: #f6f7f9;
  box-shadow: 0 0 0 1px rgb(15 23 42 / 0.09), 0 1px 2px rgb(16 24 40 / 0.05), 0 10px 24px -6px rgb(16 24 40 / 0.10),
    0 30px 70px -18px rgb(16 24 40 / 0.22);
}}
.bar {{
  position: relative; height: {BAR_H}px; display: flex; align-items: center; padding: 0 18px; background: #fdfdfe;
  border-bottom: 1px solid #e6e8ec;
}}
.bar .dots {{ gap: 8px; }}
.bar .dots i {{ width: 12px; height: 12px; }}
.bar .address {{ top: 8px; width: 420px; height: 28px; border-radius: 8px; font-size: 16px; }}
.caption {{
  position: absolute; left: {WINDOW_X + 2}px; width: {CONTENT_W - 4}px; top: {CAPTION_Y}px; height: {CAPTION_H}px;
  display: flex; align-items: center; justify-content: space-between; gap: {CAPTION_ROOM_PX}px;
}}
.caption .text {{ font-size: 30px; {MEDIUM} color: #1b2030; white-space: nowrap; }}
.caption .mode {{ font-size: 20px; color: #8a93a2; white-space: nowrap; }}
.title {{
  position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 24px;
}}
.mark {{ display: flex; align-items: center; gap: 20px; }}
.icon {{
  display: grid; place-items: center; width: 64px; height: 64px; border-radius: 18px; background: {ACCENT};
  box-shadow: inset 0 0 0 1px rgb(255 255 255 / 0.14), 0 10px 24px -8px rgb(59 91 219 / 0.5);
}}
.icon svg {{ width: 40px; height: 40px; }}
.name {{ font-size: 68px; font-weight: 700; letter-spacing: -0.02em; color: #15181d; }}
.line {{ margin: 0; font-size: 32px; color: #4a5160; }}
"""
GIF_CSS = f"""
.gif-bar {{
  position: relative; width: {GIF_W}px; height: {GIF_BAR_H}px; background: #fdfdfe; border-bottom: 1px solid #e6e8ec;
}}
.gif-bar .dots {{ position: absolute; left: 14px; top: 12px; gap: 7px; }}
.gif-bar .dots i {{ width: 10px; height: 10px; }}
.gif-bar .address {{ top: 6px; width: 300px; height: 22px; border-radius: 6px; font-size: 13px; }}
.gif-footer {{
  width: {GIF_W}px; height: {GIF_FOOTER_H}px; display: flex; align-items: center; justify-content: space-between;
  gap: {CAPTION_ROOM_PX}px; padding: 0 20px; background: #ffffff; border-top: 1px solid #e3e6eb;
}}
.gif-footer .text {{ font-size: 22px; {MEDIUM} color: #15181d; white-space: nowrap; }}
.gif-footer .mode {{ font-size: 15px; color: #7c8594; white-space: nowrap; }}
.still {{ display: block; width: {GIF_W}px; }}
"""
# The pointer: the usual arrow, black with a white edge and a soft shadow, its tip at CURSOR_TIP.
CURSOR_SIZE = (24, 28)
CURSOR_PATH = "M0 0 L0 16.2 L4.1 12.5 L6.9 18.9 L9.3 17.9 L6.6 11.6 L11.9 11.6 Z"
# Whether a caption fits its line, run on a page built by stage_html or gif_footer_html.
CAPTION_FITS_JS = f"""() => {{
  const text = document.querySelector(".text"), mode = document.querySelector(".mode");
  if (!text || !mode) return true;
  return text.getBoundingClientRect().right + {CAPTION_ROOM_PX} <= mode.getBoundingClientRect().left + 0.5;
}}"""


def _page(css: str, body: str, body_class: str = "") -> str:
    attribute = f' class="{body_class}"' if body_class else ""
    head = f'<head><meta charset="utf-8"><style>{BASE_CSS}{css}</style></head>'
    return f"<!doctype html><html>{head}<body{attribute}>{body}</body></html>"


def _bar() -> str:
    return f'<div class="dots"><i></i><i></i><i></i></div><div class="address">{html.escape(ADDRESS)}</div>'


def _line(caption: str, mode: str) -> str:
    return f'<span class="text">{html.escape(caption)}</span><span class="mode">{html.escape(mode)}</span>'


def stage_html(caption: str | None, mode: str, *, window: bool = True) -> str:
    """The mp4's stage: the backdrop, the app's window frame with an empty page area, and the caption line under it.
    With window False it is the bare backdrop a clip opens and ends on."""
    parts = []
    if window:
        parts.append(f'<div class="window"><div class="bar">{_bar()}</div></div>')
    if window and caption is not None:
        parts.append(f'<div class="caption">{_line(caption, mode)}</div>')
    return _page(STAGE_CSS, "".join(parts))


def title_html(line: str) -> str:
    """The title card: the app's name and mark, and one line on what the clip shows, on the bare backdrop."""
    paths = "".join(f'<path d="{path}"/>' for path in BRAND_PATHS)
    icon = (
        '<svg viewBox="0 0 16 16" fill="none" stroke="white" stroke-width="1.5" stroke-linecap="round" '
        f'stroke-linejoin="round">{paths}</svg>'
    )
    mark = f'<div class="mark"><span class="icon">{icon}</span><span class="name">Claims Q&amp;A</span></div>'
    return _page(STAGE_CSS, f'<div class="title">{mark}<p class="line">{html.escape(line)}</p></div>')


def gif_bar_html() -> str:
    return _page(GIF_CSS, f'<div class="gif-bar">{_bar()}</div>')


def gif_footer_html(caption: str, mode: str) -> str:
    return _page(GIF_CSS, f'<div class="gif-footer">{_line(caption, mode)}</div>')


def still_html(png_base64: str, caption: str, mode: str) -> str:
    """A still of a page, framed the way the GIF is: the browser bar above it and the caption strip under it. The
    picture is shown VIEW_W CSS pixels wide, so one captured at twice the density stays pixel for pixel."""
    shot = f'<img class="still" alt="" src="data:image/png;base64,{png_base64}">'
    bar, footer = f'<div class="gif-bar">{_bar()}</div>', f'<div class="gif-footer">{_line(caption, mode)}</div>'
    return _page(GIF_CSS, bar + shot + footer)


def cursor_html() -> str:
    """The pointer at CURSOR_SPRITE_SCALE times its size, for a screenshot with a clear background."""
    scale, (width, height) = CURSOR_SPRITE_SCALE, CURSOR_SIZE
    box = f"{-CURSOR_TIP[0]} {-CURSOR_TIP[1]} {width} {height}"
    svg = (
        f'<svg width="{width * scale}" height="{height * scale}" viewBox="{box}" '
        f'style="display: block; filter: drop-shadow(0 {scale}px {1.2 * scale}px rgb(0 0 0 / 0.32))">'
        f'<path d="{CURSOR_PATH}" fill="#111" stroke="#fff" stroke-width="2.2" stroke-linejoin="round" '
        'paint-order="stroke"/></svg>'
    )
    return f'<!doctype html><html><head><meta charset="utf-8"></head><body style="margin: 0">{svg}</body></html>'
