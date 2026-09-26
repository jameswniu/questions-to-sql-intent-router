"""The demo stage's pure logic: how a question is typed, when a caption crossfades, where the pointer and the
spotlight are at any moment, and the stage's geometry. The compositing itself is checked by looking at the rendered
files."""

import math
from pathlib import Path

import numpy as np
import pytest

from tools import demo_render as render
from tools import demo_stage as stage

HAIL = "How much did we pay on hail claims in Colorado in Q2 2025?"
ROOT = Path(__file__).resolve().parents[2]


def test_typing_is_jittered_within_its_bounds_and_the_first_key_waits_for_nothing() -> None:
    delays = stage.typing_delays(HAIL, 75)
    assert len(delays) == len(HAIL) and delays[0] == 0
    for before, delay in zip(HAIL, delays[1:], strict=False):
        extra = stage.TYPE_SPACE_MS if before == " " else stage.TYPE_PUNCT_MS if before in stage.PUNCTUATION else 0
        assert stage.TYPE_MIN_MS - 1e-9 <= delay - extra <= stage.TYPE_MAX_MS + 1e-9
    assert len({round(delay) for delay in delays[1:]}) > 10


def test_typing_takes_about_as_long_as_it_did_at_an_even_pace() -> None:
    for text in (HAIL, "Is flood damage covered?", "What's the status of claim 105964?"):
        assert sum(stage.typing_delays(text, 75)) == pytest.approx(75 * len(text), rel=0.05), text


def test_a_word_starts_after_a_longer_beat() -> None:
    delays = stage.typing_delays(HAIL, 75)
    after_space = [delay for before, delay in zip(HAIL, delays[1:], strict=False) if before == " "]
    within = [delay for before, delay in zip(HAIL, delays[1:], strict=False) if before.isalnum()]
    assert min(after_space) > sum(within) / len(within)


def test_the_same_question_always_types_the_same_way() -> None:
    assert stage.typing_delays(HAIL, 75) == stage.typing_delays(HAIL, 75)
    assert stage.typing_delays(HAIL, 75) != stage.typing_delays(HAIL.lower(), 75)
    assert stage.typing_delays("", 75) == [] and stage.typing_delays("2", 75) == [0.0]
    assert len(stage.typing_delays("2025", 75)) == 4


def test_a_caption_crossfades_over_its_fade_and_the_first_is_there_from_the_start() -> None:
    changes = [(0.0, "Dana asks about her region"), (5.2, "Ask for a paid-loss figure")]
    assert stage.caption_at(changes, 0.0) == ("Dana asks about her region", "Dana asks about her region", 1.0)
    assert stage.caption_at(changes, 5.2) == ("Dana asks about her region", "Ask for a paid-loss figure", 0.0)
    old, new, k = stage.caption_at(changes, 5.2 + stage.CAPTION_FADE_S / 2)
    assert (old, new) == ("Dana asks about her region", "Ask for a paid-loss figure") and 0 < k < 1
    assert stage.caption_at(changes, 5.2 + stage.CAPTION_FADE_S)[2] == 1.0
    assert stage.caption_at([], 3.0) == (None, None, 1.0)


def test_a_glide_takes_half_a_second_to_seven_tenths_by_distance() -> None:
    assert stage.glide_seconds(10) == pytest.approx(stage.GLIDE_MIN_S, abs=0.01)
    assert stage.glide_seconds(5000) == stage.GLIDE_MAX_S
    assert stage.glide_seconds(200) < stage.glide_seconds(400)


def test_a_glide_eases_in_and_out_on_a_gentle_arc_and_lands_exactly() -> None:
    start, end = (100.0, 500.0), (700.0, 200.0)
    assert stage.glide_point(start, end, 0) == start
    assert stage.glide_point(start, end, 1) == pytest.approx(end)
    middle = stage.glide_point(start, end, 0.5)
    straight = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    length = math.dist(start, end)
    assert 0 < math.dist(middle, straight) <= stage.GLIDE_ARC * length
    first = math.dist(start, stage.glide_point(start, end, 0.05))
    halfway = math.dist(stage.glide_point(start, end, 0.475), stage.glide_point(start, end, 0.525))
    assert first < halfway / 4


def test_the_pointer_shows_from_a_glide_hides_to_type_and_fades_when_left_alone() -> None:
    cursor = stage.CursorTrack(
        [
            ["glide", 5.0, 5.6, 576.0, 330.0, 450.0, 590.0],
            ["press", 5.6, 450.0, 590.0],
            ["type", 5.8, 10.0],
            ["glide", 10.0, 10.6, 450.0, 590.0, 820.0, 596.0],
            ["press", 11.0, 820.0, 596.0],
        ]
    )
    assert cursor.opacity(4.9) == 0
    assert cursor.opacity(5.6) == 1 and cursor.point(5.6) == (450.0, 590.0)
    assert cursor.opacity(8.0) == 0  # typing
    assert cursor.point(10.0) == (450.0, 590.0) and cursor.opacity(10.3) == 1
    assert cursor.opacity(11.0 + stage.CURSOR_IDLE_S - 0.01) == 1
    assert cursor.opacity(11.0 + stage.CURSOR_IDLE_S + stage.CURSOR_FADE_S) == 0
    [(where, progress)] = cursor.ripples(11.1)
    assert where == (820.0, 596.0) and progress == pytest.approx(0.1 / stage.RIPPLE_S)
    assert cursor.ripples(11.0 + stage.RIPPLE_S) == []


def test_the_pointer_steps_away_once_a_reading_pause_starts() -> None:
    cursor = stage.CursorTrack(
        [["glide", 5.0, 5.6, 576.0, 330.0, 820.0, 596.0], ["press", 5.6, 820.0, 596.0], ["rest", 6.0]]
    )
    assert cursor.opacity(5.9) == 1
    assert cursor.opacity(6.0 + stage.CURSOR_FADE_S) == 0
    assert cursor.ripples(6.0)  # the click's ripple plays out whatever the pointer does


def test_a_press_with_no_glide_before_it_never_pops_up() -> None:
    assert stage.CursorTrack([["press", 2.0, 100.0, 100.0]]).opacity(2.1) == 0


RECT_A = (60.0, 200.0, 840.0, 300.0)
RECT_B = (70.0, 330.0, 830.0, 520.0)


def test_the_spotlight_closes_in_as_it_fades_in_then_glides_between_rects() -> None:
    spot = stage.SpotlightTrack([(1.0, "focus", RECT_A, 546.0), (5.0, "focus", RECT_B, 546.0)])
    assert spot.at(0.5) == (None, 0.0)
    rect, opacity = spot.at(1.0)
    assert rect == stage.grow(RECT_A, stage.SPOT_GROW) and opacity == 0
    assert spot.at(1.0 + stage.SPOT_FADE_S) == (RECT_A, 1.0)
    rect, opacity = spot.at(5.0 + stage.SPOT_GLIDE_S / 2)
    assert rect is not None and RECT_A[1] < rect[1] < RECT_B[1] and opacity == 1
    assert spot.at(5.0 + stage.SPOT_GLIDE_S) == (RECT_B, 1.0)


def test_a_cleared_spotlight_fades_out_where_it_was_and_a_moved_rect_is_followed() -> None:
    spot = stage.SpotlightTrack(
        [(1.0, "focus", RECT_A, 546.0), (3.0, "clear", None, None), (4.0, "focus", RECT_B, 636.0)]
    )
    rect, opacity = spot.at(3.0 + stage.SPOT_FADE_S / 2)
    assert rect == RECT_A and 0 < opacity < 1
    assert spot.at(3.0 + stage.SPOT_FADE_S)[1] == 0
    moved = (RECT_B[0], RECT_B[1] - 40, RECT_B[2], RECT_B[3] - 40)
    followed = stage.SpotlightTrack([(1.0, "focus", RECT_B, 546.0), (2.0, "track", moved, 546.0)])
    assert followed.at(2.0) == (moved, 1.0)


def test_the_floor_is_the_one_the_page_last_reported() -> None:
    spot = stage.SpotlightTrack(
        [(1.0, "focus", RECT_A, 546.0), (3.0, "clear", None, None), (4.0, "focus", RECT_B, 636.0)]
    )
    assert [spot.floor_at(t) for t in (0.0, 2.0, 3.5, 5.0)] == [546.0, 546.0, 546.0, 636.0]
    assert stage.SpotlightTrack([(1.0, "focus", RECT_A, None)]).floor_at(2.0) is None


def test_the_frame_on_screen_is_the_last_taken_by_then() -> None:
    times = [-0.05, 0.4, 0.43, 2.0]
    assert [stage.frame_at(times, t) for t in (-1.0, 0.0, 0.4, 0.42, 1.9, 9.0)] == [0, 0, 1, 1, 2, 3]


def test_the_stage_scales_the_app_text_to_24_px_and_fits_the_frame() -> None:
    assert 18 * stage.CONTENT_SCALE >= 24
    shape = stage.CONTENT_W / stage.CONTENT_H
    assert shape == pytest.approx(stage.VIEW_W / stage.VIEW_H, rel=1e-3)
    assert stage.WINDOW_Y > 0 and stage.CAPTION_Y + stage.CAPTION_H < stage.STAGE_H
    assert stage.CONTENT_X + stage.CONTENT_W < stage.STAGE_W
    assert (stage.GIF_W, stage.GIF_H) == (900, stage.GIF_BAR_H + stage.VIEW_H + stage.GIF_FOOTER_H)


def test_the_title_card_uses_the_app_brand_mark() -> None:
    css = (ROOT / "app" / "web" / "static" / "style.css").read_text()
    for path in stage.BRAND_PATHS:
        assert path in css
    assert "Claims Q&amp;A" in stage.title_html("A paid-loss figure, traced to its SQL")


def test_the_mode_label_is_on_the_stage_and_in_the_gif_footer() -> None:
    assert ">No API key<" in stage.stage_html("Read the returned figure", "No API key")
    assert ">No API key<" in stage.gif_footer_html("Read the returned figure", "No API key")
    assert stage.ADDRESS in stage.stage_html(None, "No API key") and stage.ADDRESS in stage.gif_bar_html()


def test_a_still_is_framed_like_the_gif() -> None:
    page = stage.still_html("iVBORw0KGgo=", "Priya opens the operator dashboard", "No API key")
    assert page.index('class="gif-bar"') < page.index('class="still"') < page.index('class="gif-footer"')
    assert "data:image/png;base64,iVBORw0KGgo=" in page and ">Priya opens the operator dashboard<" in page


def test_the_cutout_covers_its_rect_with_rounded_anti_aliased_corners() -> None:
    cover = render.rounded_cover(100, 80, (20.0, 10.0, 80.0, 70.0), 12.0)
    assert cover[40, 50] == 1 and cover[5, 50] == 0 and cover[40, 90] == 0
    assert cover[10, 20] == 0  # the corner is cut off
    # An edge through the middle of a pixel covers half of it.
    assert render.rounded_cover(100, 80, (20.0, 10.5, 80.0, 70.0), 12.0)[10, 50] == pytest.approx(0.5)


def test_what_the_floor_cuts_off_dissolves_outside_the_cutout_only() -> None:
    pixels = np.zeros((100, 60, 3), np.uint8)
    cover = render.rounded_cover(60, 100, (0.0, 50.0, 30.0, 80.0), 0.0)  # the left half, from row 50 to the floor
    faded = render.fade_floor(pixels, 80.0, 40.0, cover, 1.0)
    assert (faded[:40] == 0).all() and (faded[80:] == 0).all()  # above its reach, and the composer itself
    assert (faded[60:80, :30] == 0).all()  # what is being read stays whole
    assert faded[79, 45].tolist() == [246, 247, 249] or faded[79, 45, 0] > 240  # dissolved at the floor
    assert 0 < faded[60, 45, 0] < faded[75, 45, 0]  # eased in towards the floor
    assert (render.fade_floor(pixels, 80.0, 40.0, cover, 0.0) == 0).all()  # no spotlight, no fade


def test_the_spotlight_leaves_its_cutout_alone_and_dims_the_rest() -> None:
    pixels = np.full((80, 100, 3), 255, np.uint8)
    dimmed = render.dim(pixels, (20.0, 10.0, 80.0, 70.0), 12.0, 1.0)
    assert (dimmed[40, 50] == 255).all()
    expected = [round(255 * (1 - stage.SPOT_DIM) + tint * stage.SPOT_DIM) for tint in stage.SPOT_TINT]
    assert dimmed[2, 2].tolist() == expected
