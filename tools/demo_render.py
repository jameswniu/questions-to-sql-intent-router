"""Composites a recorded demo clip into its finished files: the mp4 on its stage, the README's GIF and a poster.

The recorder leaves, for each clip, the page frames it captured (JPEGs at twice the pixel density, each stamped with
when it was taken), the chrome its Chromium drew (the stage for each caption line, the title card, the bare stage,
the GIF's bar and footers, the pointer) and a log of what happened when. Every output frame is built for its own
moment: the page as last captured by then, with the spotlight, the ripple and the pointer drawn over it and the
chrome around it. So a glide or a fade is as smooth as the output's frame rate, whatever rate
the page was captured at.

Host only: it needs numpy and Pillow, which the recording container doesn't have.
"""

import json
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw

try:
    from tools import demo_stage as stage
except ModuleNotFoundError:  # run as a script from tools/, where the repository root may not be on the path
    import demo_stage as stage  # type: ignore[import-not-found, no-redef]

Pixels = npt.NDArray[np.uint8]
Cover = npt.NDArray[np.float32]
CHROME = "chrome.json"
LANCZOS = Image.Resampling.LANCZOS
X264 = ("-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart")
# RGB to limited-range BT.709 in the same chain as the format, so the matrix used is the one the file is tagged with,
# and the frames tagged BT.709 throughout, which output options alone don't do once a filter has set the frames' own.
TO_YUV = (
    "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
    "setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"
)
# One palette per GIF from every frame's pixels, no dithering so text keeps clean edges, and only the rectangle that
# changed is written for each frame.
GIF_FILTER = (
    "split[a][b];[a]palettegen=max_colors={colors}:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle"
)


class RenderError(Exception):
    """A clip could not be composited or encoded."""


@dataclass(frozen=True)
class Plan:
    """A recorded clip's log, as the recorder wrote it, with times in seconds from the clip's start."""

    length_s: float
    mode: str
    frames: list[tuple[float, str]]
    captions: list[tuple[float, str]]
    spot: list[tuple[float, str, stage.Rect | None, float | None]]
    cursor: list[list[Any]]

    @classmethod
    def load(cls, log: dict[str, Any], length_s: float) -> "Plan":
        def four(value: Sequence[Any]) -> stage.Rect:
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))

        return cls(
            length_s=length_s,
            mode=str(log["mode"]),
            frames=[(float(at), str(name)) for at, name in log["frames"]],
            captions=[(float(at), str(text)) for at, text in log["captions"]],
            spot=[spot_entry(entry) for entry in log["spot"]],
            cursor=[list(event) for event in log["cursor"]],
        )


def spot_entry(entry: Sequence[Any]) -> tuple[float, str, stage.Rect | None, float | None]:
    """One spotlight log entry: its time, its kind, its rect, and the page's floor when the page reported one."""
    at, kind, rect = entry[0], entry[1], entry[2]
    floor = entry[3] if len(entry) > 3 else None
    box = None if rect is None else (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
    return float(at), str(kind), box, None if floor is None else float(floor)


def load_rgb(path: Path) -> Pixels:
    with Image.open(path) as image:
        return np.array(image.convert("RGB"), dtype=np.uint8)


class Chrome:
    """The pictures the recorder's Chromium drew for a clip, listed in its chrome.json."""

    def __init__(self, folder: Path) -> None:
        index = json.loads((folder / CHROME).read_text())
        self.stages = {text: load_rgb(folder / name) for text, name in index["stages"].items()}
        self.footers = {text: load_rgb(folder / name) for text, name in index["footers"].items()}
        self.window = load_rgb(folder / index["window"])
        self.title = load_rgb(folder / index["title"])
        self.empty = load_rgb(folder / index["empty"])
        self.bar = load_rgb(folder / index["bar"])
        with Image.open(folder / index["cursor"]) as cursor:
            self.cursor = cursor.convert("RGBA")


class Frames:
    """The page frames the recorder captured, decoded when first shown."""

    def __init__(self, folder: Path, frames: list[tuple[float, str]]) -> None:
        if not frames:
            raise RenderError(f"no page frames were captured into {folder}")
        self.folder = folder
        self.times = [at for at, _ in frames]
        self.names = [name for _, name in frames]
        self.decoded: dict[int, Image.Image] = {}

    def index(self, t: float) -> int:
        return stage.frame_at(self.times, t)

    def image(self, index: int) -> Image.Image:
        if index not in self.decoded:
            if len(self.decoded) >= 4:
                del self.decoded[next(iter(self.decoded))]
            with Image.open(self.folder / self.names[index]) as image:
                self.decoded[index] = image.convert("RGB")
        return self.decoded[index]


def rounded_cover(width: int, height: int, rect: stage.Rect, radius: float) -> Cover:
    """How much of each pixel of a width by height picture lies inside rect with rounded corners, from 0 to 1,
    anti-aliased along the edge."""
    cover = np.zeros((height, width), np.float32)
    left, top, right, bottom = rect
    x0, y0 = max(0, int(left) - 1), max(0, int(top) - 1)
    x1, y1 = min(width, int(right) + 2), min(height, int(bottom) + 2)
    if x1 <= x0 or y1 <= y0 or right <= left or bottom <= top:
        return cover
    half_w, half_h = (right - left) / 2, (bottom - top) / 2
    r = min(radius, half_w, half_h)
    qx = np.abs(np.arange(x0, x1, dtype=np.float32) + 0.5 - (left + half_w)) - (half_w - r)
    qy = np.abs(np.arange(y0, y1, dtype=np.float32) + 0.5 - (top + half_h)) - (half_h - r)
    across, down = qx[None, :], qy[:, None]
    outside = np.hypot(np.maximum(across, 0), np.maximum(down, 0))
    inside = np.minimum(np.maximum(across, down), 0)
    cover[y0:y1, x0:x1] = np.clip(0.5 - (outside + inside - r), 0, 1)
    return cover


def shade(pixels: Pixels, cover: Cover, opacity: float) -> Pixels:
    """The spotlight: everything the cutout doesn't cover darkened by SPOT_DIM times opacity towards SPOT_TINT."""
    amount = (opacity * stage.SPOT_DIM) * (1 - cover)
    tint = np.array(stage.SPOT_TINT, np.float32)
    mixed = pixels.astype(np.float32) * (1 - amount[..., None]) + tint * amount[..., None]
    return np.rint(mixed).astype(np.uint8)


def dim(pixels: Pixels, rect: stage.Rect, radius: float, opacity: float) -> Pixels:
    height, width = pixels.shape[:2]
    return shade(pixels, rounded_cover(width, height, rect, radius), opacity)


def fade_floor(pixels: Pixels, floor: float, reach: float, cover: Cover, opacity: float) -> Pixels:
    """Dissolves what lies within reach pixels above the page's floor into the page's background, eased in towards
    the floor, outside the cutout only, so a line the composer or the window's edge cuts off never peeks out while
    what is being read stays whole. Rows from the floor down are the composer's own, and are left alone."""
    top, bottom = max(0, int(floor - reach)), min(pixels.shape[0], int(np.ceil(floor)))
    if bottom <= top or opacity <= 0:
        return pixels
    rows = np.arange(top, bottom, dtype=np.float32) + 0.5
    k = np.clip((rows - (floor - reach)) / reach, 0, 1)
    ramp = k * k * (3 - 2 * k)
    amount = (opacity * ramp[:, None] * (1 - cover[top:bottom]))[..., None]
    out = pixels.copy()
    band = out[top:bottom].astype(np.float32)
    out[top:bottom] = np.rint(band * (1 - amount) + np.array(stage.CANVAS, np.float32) * amount).astype(np.uint8)
    return out


def blend(a: Pixels, b: Pixels, k: float) -> Pixels:
    if k <= 0:
        return a
    if k >= 1:
        return b
    return np.rint(a.astype(np.float32) * (1 - k) + b.astype(np.float32) * k).astype(np.uint8)


def overlay(pixels: Pixels, sprite: Image.Image, x: float, y: float, opacity: float) -> None:
    """Draws an RGBA sprite over pixels in place, its top left corner at x, y, faded by opacity."""
    left, top = round(x), round(y)
    height, width = pixels.shape[:2]
    x0, y0 = max(0, left), max(0, top)
    x1, y1 = min(width, left + sprite.width), min(height, top + sprite.height)
    if x1 <= x0 or y1 <= y0 or opacity <= 0:
        return
    rgba = np.asarray(sprite, dtype=np.float32)[y0 - top : y1 - top, x0 - left : x1 - left]
    alpha = rgba[..., 3:4] / 255 * opacity
    under = pixels[y0:y1, x0:x1].astype(np.float32)
    pixels[y0:y1, x0:x1] = np.rint(under * (1 - alpha) + rgba[..., :3] * alpha).astype(np.uint8)


def resized(sprite: Image.Image, size: tuple[int, int]) -> Image.Image:
    """An RGBA picture at another size, resampled with its alpha premultiplied so its edges don't darken."""
    return sprite.convert("RGBa").resize(size, LANCZOS).convert("RGBA")


def ripple(progress: float, scale: float) -> Image.Image:
    """One moment of a click's ripple, a soft white disc with a ring in the app's accent that grows and fades, so it
    shows on a white control and on an accent one alike. Drawn four times over and scaled down so its edge is smooth,
    with its centre at the picture's centre."""
    over = 4
    grown = stage.lerp(*stage.RIPPLE_RADIUS, stage.ease_out(progress)) * scale
    fade = (1 - progress) ** 1.5
    size = 2 * round(stage.RIPPLE_RADIUS[1] * scale + 4)
    big = Image.new("RGBA", (size * over, size * over), (0, 0, 0, 0))
    middle, radius = size * over / 2, grown * over
    ring = max(1, round(2 * scale * over))
    color = stage.RIPPLE_COLOR
    ImageDraw.Draw(big).ellipse(
        (middle - radius, middle - radius, middle + radius, middle + radius),
        fill=(255, 255, 255, round(110 * fade)),
        outline=(*color, round(210 * fade)),
        width=ring,
    )
    return resized(big, (size, size))


class Scene:
    """A clip's page at any moment and at any size: the captured frame, with the spotlight, the click ripples and the
    pointer drawn over it."""

    def __init__(self, plan: Plan, frames: Frames, chrome: Chrome) -> None:
        self.plan, self.frames, self.chrome = plan, frames, chrome
        self.spot = stage.SpotlightTrack(plan.spot)
        self.cursor = stage.CursorTrack(plan.cursor)
        self.pointers: dict[float, Image.Image] = {}
        self.last: tuple[tuple[Any, ...], Pixels] | None = None
        self.key: tuple[Any, ...] = ()  # what the last page drawn shows, so a caller can reuse what it built on it

    def pointer(self, scale: float) -> Image.Image:
        """The pointer sprite at scale pixels per CSS pixel."""
        key = round(scale, 3)
        if key not in self.pointers:
            factor = scale / stage.CURSOR_SPRITE_SCALE
            sprite = self.chrome.cursor
            self.pointers[key] = resized(sprite, (round(sprite.width * factor), round(sprite.height * factor)))
        return self.pointers[key]

    def page(self, t: float, size: tuple[int, int]) -> Pixels:
        scale = size[0] / stage.VIEW_W  # output pixels per CSS pixel
        index = self.frames.index(t)
        rect, dimmed = self.spot.at(t)
        floor = self.spot.floor_at(t)
        shown = self.cursor.opacity(t)
        point = self.cursor.point(t)
        ripples = self.cursor.ripples(t)
        key = (
            index,
            size,
            None if rect is None or dimmed < 0.001 else tuple(round(value, 2) for value in rect),
            round(dimmed, 3),
            floor,
            tuple(round(value, 1) for value in point) if shown > 0 else None,
            round(shown, 3),
            tuple((spot, round(progress, 3)) for spot, progress in ripples),
        )
        self.key = key
        if self.last is not None and self.last[0] == key:
            return self.last[1]

        def place(x: float, y: float) -> tuple[float, float]:
            return x * scale, y * scale

        pixels: Pixels = np.array(self.frames.image(index).resize(size, LANCZOS), dtype=np.uint8)
        if rect is not None and dimmed >= 0.001:
            corner, far = place(rect[0], rect[1]), place(rect[2], rect[3])
            cover = rounded_cover(size[0], size[1], (*corner, *far), stage.SPOT_RADIUS * scale)
            if floor is not None:
                pixels = fade_floor(pixels, floor * scale, stage.FLOOR_FADE_PX * scale, cover, dimmed)
            pixels = shade(pixels, cover, dimmed)
        for (x, y), progress in ripples:
            sprite = ripple(progress, scale)
            middle = place(x, y)
            overlay(pixels, sprite, middle[0] - sprite.width / 2, middle[1] - sprite.height / 2, 1.0)
        if shown > 0:
            sprite = self.pointer(scale)
            tip = place(*point)
            overlay(pixels, sprite, tip[0] - stage.CURSOR_TIP[0] * scale, tip[1] - stage.CURSOR_TIP[1] * scale, shown)
        self.last = (key, pixels)
        return pixels


class Mp4Frames:
    """The mp4's frames: the title card, the window on its stage with the caption line under it, and the fade back
    to the bare stage."""

    def __init__(self, scene: Scene) -> None:
        self.scene, self.plan, self.chrome = scene, scene.plan, scene.chrome
        # The window's bottom corners are rounded, so the page is cut to them where it meets the frame.
        rect = (0.0, -2.0 * stage.WINDOW_RADIUS, float(stage.CONTENT_W), float(stage.CONTENT_H))
        self.cover = rounded_cover(stage.CONTENT_W, stage.CONTENT_H, rect, stage.WINDOW_RADIUS)[..., None]
        self.last: tuple[tuple[Any, ...], Pixels] | None = None

    def count(self) -> int:
        return round((stage.TITLE_S + self.plan.length_s + stage.OUTRO_S) * stage.FPS)

    def backdrop(self, t: float) -> tuple[tuple[Any, ...], Pixels]:
        """The stage with its caption line at t, crossfading between two captions, and a key naming that state."""
        old, new, k = stage.caption_at(self.plan.captions, t)
        if new is None:
            return ("window",), self.chrome.window
        if old == new or k >= 1 or old is None:
            return (new,), self.chrome.stages[new]
        mixed = self.chrome.stages[new].copy()
        band = slice(stage.CAPTION_Y - 12, stage.CAPTION_Y + stage.CAPTION_H + 12)
        mixed[band] = blend(self.chrome.stages[old][band], self.chrome.stages[new][band], k)
        return (old, new, round(k, 3)), mixed

    def window(self, t: float) -> Pixels:
        state, base = self.backdrop(t)
        page = self.scene.page(t, (stage.CONTENT_W, stage.CONTENT_H))
        key = (state, self.scene.key)
        if self.last is not None and self.last[0] == key:
            return self.last[1]
        out = base.copy()
        top, left = stage.CONTENT_Y, stage.CONTENT_X
        area = (slice(top, top + stage.CONTENT_H), slice(left, left + stage.CONTENT_W))
        out[area] = np.rint(page * self.cover + base[area] * (1 - self.cover)).astype(np.uint8)
        self.last = (key, out)
        return out

    def frame(self, number: int) -> Pixels:
        t = number / stage.FPS
        if t < stage.TITLE_S:
            if t < stage.TITLE_FADE_S:
                return blend(self.chrome.empty, self.chrome.title, stage.ease(t / stage.TITLE_FADE_S))
            fading = stage.TITLE_S - stage.WINDOW_FADE_S
            if t < fading:
                return self.chrome.title
            return blend(self.chrome.title, self.window(0.0), stage.ease((t - fading) / stage.WINDOW_FADE_S))
        clip = min(t - stage.TITLE_S, self.plan.length_s)
        shown = self.window(clip)
        past = t - stage.TITLE_S - self.plan.length_s
        return shown if past <= 0 else blend(shown, self.chrome.empty, stage.ease(past / stage.OUTRO_S))


def gif_frame(scene: Scene, t: float) -> Pixels:
    """One frame of the GIF: the bar, the page full width at its own size, and the caption footer."""
    out = np.empty((stage.GIF_H, stage.GIF_W, 3), np.uint8)
    out[: stage.GIF_BAR_H] = scene.chrome.bar
    out[stage.GIF_BAR_H : stage.GIF_BAR_H + stage.VIEW_H] = scene.page(t, (stage.VIEW_W, stage.VIEW_H))
    old, new, k = stage.caption_at(scene.plan.captions, t)
    footers = scene.chrome.footers
    if new is not None:
        footer = footers[new] if old is None or old == new else blend(footers[old], footers[new], k)
        out[stage.GIF_BAR_H + stage.VIEW_H :] = footer
    else:
        out[stage.GIF_BAR_H + stage.VIEW_H :] = 255
    return out


def encode(command: list[str], frames: Iterator[bytes]) -> None:
    """Pipes raw RGB frames into an ffmpeg command."""
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stderr is not None
    try:
        for frame in frames:
            process.stdin.write(frame)
    except BrokenPipeError:
        pass
    finally:
        process.stdin.close()
    message = process.stderr.read().decode(errors="replace").strip()
    if process.wait():
        raise RenderError(f"ffmpeg stopped: {message[-600:]}")


def raw_frames(width: int, height: int, fps: int) -> list[str]:
    """An ffmpeg command's start, reading raw RGB frames of this size and rate from its standard input."""
    size, rate = f"{width}x{height}", str(fps)
    start = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    return [*start, "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", size, "-framerate", rate, "-i", "-"]


def scene_for(plan: Plan, frames_dir: Path, chrome_dir: Path) -> Scene:
    return Scene(plan, Frames(frames_dir, plan.frames), Chrome(chrome_dir))


def write_mp4(plan: Plan, frames_dir: Path, chrome_dir: Path, out: Path) -> None:
    """The mp4: 1920 by 1080 at FPS, H.264 in yuv420p tagged BT.709, with the index up front for streaming."""
    video = Mp4Frames(scene_for(plan, frames_dir, chrome_dir))
    command = [*raw_frames(stage.STAGE_W, stage.STAGE_H, stage.FPS), "-vf", TO_YUV, *X264, str(out)]
    encode(command, (video.frame(n).tobytes() for n in range(video.count())))


def write_poster(plan: Plan, frames_dir: Path, chrome_dir: Path, at_s: float, out: Path) -> None:
    """The clip at at_s into it, framed like the GIF, since the README shows posters in the same narrow column. It
    comes straight from the compositor, so it never carries a video's blur."""
    Image.fromarray(gif_frame(scene_for(plan, frames_dir, chrome_dir), at_s)).save(out, optimize=True)


def write_gif(
    plan: Plan, frames_dir: Path, chrome_dir: Path, out: Path, tries: Sequence[tuple[int, int]], limit_bytes: int
) -> list[str]:
    """The GIF at the first (frames per second, colours) in tries that comes in at or under limit_bytes, so colours
    go before frames per second do. Each rate is composited once into a lossless file the palettes are cut from.
    Returns what each try weighed; the file left at out is the last one tried."""
    scene = scene_for(plan, frames_dir, chrome_dir)
    tried: list[str] = []
    with tempfile.TemporaryDirectory(prefix="demo-gif-") as scratch:
        lossless: dict[int, Path] = {}
        for fps, colors in tries:
            if fps not in lossless:
                lossless[fps] = Path(scratch) / f"{fps}.mkv"
                count = round(plan.length_s * fps)
                frames = (gif_frame(scene, n / fps).tobytes() for n in range(count))
                encode([*raw_frames(stage.GIF_W, stage.GIF_H, fps), "-c:v", "ffv1", str(lossless[fps])], frames)
            done = subprocess.run(
                [
                    *("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(lossless[fps])),
                    *("-vf", GIF_FILTER.format(colors=colors), "-loop", "0", str(out)),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if done.returncode:
                raise RenderError(f"ffmpeg could not write the GIF: {done.stderr.strip()[-600:]}")
            size = out.stat().st_size
            tried.append(f"{size / 1_000_000:.2f} MB at {fps} fps and {colors} colours")
            if size <= limit_bytes:
                break
    return tried
