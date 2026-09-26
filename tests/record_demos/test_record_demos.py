"""The demo recorder's pure logic: how long a reading pause lasts, how long a caption may be, which files each clip
writes and what each may weigh. Recording itself needs the stack and a browser, so it is checked by the recorder's
own assertions, not here."""

import json
from pathlib import Path

import pytest

from tools import record_demos as rd

VIDEO_CLIPS = [clip for clip in rd.OUTPUTS if clip != "dashboard-still"]


def test_a_pause_is_never_shorter_than_its_storyboard_minimum() -> None:
    assert rd.reading_hold(8.0, 12, "Read the returned figure") == 8.0
    assert rd.reading_hold(8.0, 0, "") == 8.0


def test_a_pause_grows_with_the_words_on_screen_and_in_the_caption() -> None:
    # 40 words on screen and 4 in the caption: 2.0 + 0.30 * 44.
    assert rd.reading_hold(2.0, 40, "Read the returned figure") == pytest.approx(15.2)
    assert rd.reading_hold(2.0, 41, "Read the returned figure") > rd.reading_hold(2.0, 40, "Read the returned figure")


def test_a_table_is_read_cell_by_cell_when_that_takes_longer() -> None:
    # 10 numbers, 5 header words and a 2-word caption: 2.0 + 0.6 * 10 + 0.30 * 7, longer than 2.0 + 0.30 * 9.
    assert rd.reading_hold(0.0, 7, "Check shares", cells=10, labels=5) == pytest.approx(10.1)
    assert rd.reading_hold(0.0, 40, "Check shares", cells=1, labels=5) == pytest.approx(2.0 + 0.3 * 42)


def test_one_constant_retimes_every_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    before = rd.reading_hold(0.0, 20, "Read the answer")
    monkeypatch.setattr(rd, "SECONDS_PER_WORD", 0.60)
    assert rd.reading_hold(0.0, 20, "Read the answer") == pytest.approx(2.0 + (before - 2.0) * 2)


def test_sql_is_read_in_names_placeholders_and_operators() -> None:
    sql = "SELECT SUM(amount) AS value FROM sem.v_payments_net WHERE (paid_date BETWEEN %s AND %s) AND state = ANY(%s)"
    # SELECT SUM amount AS value FROM sem.v_payments_net WHERE paid_date BETWEEN %s AND %s AND state = ANY %s
    assert rd.sql_units(sql) == 18


def test_a_caption_is_one_to_eight_words() -> None:
    rd.check_caption("one two three four five six seven eight")
    for bad in ("one two three four five six seven eight nine", "", "   "):
        with pytest.raises(ValueError):
            rd.check_caption(bad)


def test_every_caption_fits_the_word_limit() -> None:
    for clip, beats in rd.CAPTIONS.items():
        for beat, caption in beats.items():
            text = caption.format(claim=105964)
            assert rd.caption_words(text) <= rd.CAPTION_MAX_WORDS, f"{clip}.{beat}: {text!r}"
            rd.check_caption(text)


def test_every_video_clip_has_captions_and_opens_on_who_is_asking() -> None:
    assert set(rd.CAPTIONS) == set(VIDEO_CLIPS)
    for clip in VIDEO_CLIPS:
        assert "who" in rd.CAPTIONS[clip], clip


def test_outputs_and_clips_name_the_same_clips() -> None:
    assert set(rd.OUTPUTS) == set(rd.CLIPS)
    assert len(VIDEO_CLIPS) == 11


def test_each_video_clip_writes_an_mp4_and_a_poster_and_only_ask_a_gif() -> None:
    for clip in VIDEO_CLIPS:
        files = rd.OUTPUTS[clip]
        assert f"{clip}.mp4" in files and f"{clip}.poster.png" in files, clip
        assert any(name.endswith(".gif") for name in files) == (clip == "ask"), clip
    assert rd.OUTPUTS["dashboard-still"] == ("dashboard.png",)


def test_no_file_is_written_by_two_clips_and_retired_files_are_gone() -> None:
    names = [name for files in rd.OUTPUTS.values() for name in files]
    assert len(names) == len(set(names))
    assert not {"edges.mp4", "permissions.gif"} & set(names)


def test_the_dashboard_is_recorded_after_every_chat_clip() -> None:
    # Its UI filter counts the chat clips' own requests, so it and its still come last.
    assert list(rd.OUTPUTS)[-2:] == ["dashboard", "dashboard-still"]


def test_every_output_has_a_budget() -> None:
    assert set(rd.BUDGETS) == {name for files in rd.OUTPUTS.values() for name in files}


def test_the_budgets_follow_the_storyboard() -> None:
    assert rd.BUDGETS["ask.gif"] == rd.Budget(4 * rd.MB, 5 * rd.MB, 52)
    assert rd.BUDGETS["ask.mp4"].limit_bytes == 10 * rd.MB
    for clip in rd.BOUNDARY_CLIPS:
        assert rd.BUDGETS[f"{clip}.mp4"] == rd.Budget(3 * rd.MB, 6 * rd.MB, 120)
    for clip in rd.EVIDENCE_CLIPS:
        assert rd.BUDGETS[f"{clip}.mp4"] == rd.Budget(8 * rd.MB, 15 * rd.MB, 120)
    assert rd.BUDGETS["dashboard.png"].limit_bytes == 1 * rd.MB


def test_a_file_over_its_limit_fails_and_one_over_its_target_warns() -> None:
    assert rd.check_budget("ask.gif", 4 * rd.MB, 45.0) == (None, None)
    failed, warned = rd.check_budget("ask.gif", 5 * rd.MB + 1, 45.0)
    assert failed and "limit" in failed and warned is None
    failed, warned = rd.check_budget("ask.gif", 3 * rd.MB, 52.5)
    assert failed and "52 s" in failed
    failed, warned = rd.check_budget("injection.mp4", 4 * rd.MB, 25.0)
    assert failed is None and warned and "target" in warned
    assert rd.check_budget("ask.poster.png", 400_000, None) == (
        None,
        "ask.poster.png is 0.40 MB, over its 0.25 MB target",
    )


def test_amounts_read_the_way_the_answers_write_them() -> None:
    assert rd.dollars("4108452.79") == "$4,108,453"
    assert rd.dollars("0.50") == "$1"
    assert rd.cents("45028.54") == "$45,028.54"


def test_eval_ids_are_the_cases_that_ask_exactly_that_question() -> None:
    known = [{"id": "b-2", "q": "Is flood damage covered?"}, {"id": "a-1", "q": "Is flood damage covered?"}]
    known.append({"id": "c-3", "q": "Is flood damage coverde?"})
    assert rd.eval_ids("Is flood damage covered?", known) == ["a-1", "b-2"]
    assert rd.eval_ids("What's a good recipe for banana bread?", known) == []


def test_the_manifest_keeps_other_clips_and_lists_clips_in_recording_order(tmp_path: Path) -> None:
    rd.write_manifest(tmp_path, {"why": {"length_s": 95.0}, "ask": {"length_s": 42.0}})
    rd.write_manifest(tmp_path, {"why": {"length_s": 96.0}})
    manifest = json.loads((tmp_path / rd.MANIFEST).read_text())
    assert list(manifest["clips"]) == ["ask", "why"]
    assert manifest["clips"]["why"] == {"length_s": 96.0}
    assert manifest["pacing"]["seconds_per_word"] == rd.SECONDS_PER_WORD


def test_the_page_script_gets_every_setting_it_names() -> None:
    assert "__" not in rd.demo_js()


def test_every_video_clip_opens_on_a_title_line() -> None:
    assert set(rd.TITLES) == set(VIDEO_CLIPS)
    for clip, title in rd.TITLES.items():
        assert 0 < rd.caption_words(title) <= rd.CAPTION_MAX_WORDS, clip


def test_on_screen_text_keeps_the_voice_rules() -> None:
    shown = [text for beats in rd.CAPTIONS.values() for text in beats.values()] + list(rd.TITLES.values())
    for text in shown:
        for banned in ("\u2014", "\u2013", "--", "\u2192", "showcase", "production-grade", "golden", "source of truth"):
            assert banned not in text.lower(), text


def test_each_output_comes_out_its_own_width() -> None:
    # The README's column shows the GIF, the posters and the still, so they share the GIF's framing.
    assert rd.width_of("ask.mp4") == rd.width_of("policy.mp4") == 1920
    assert rd.width_of("ask.gif") == rd.width_of("ask.poster.png") == rd.width_of("permissions.poster.png") == 900
    assert rd.width_of("dashboard.png") == 1800


def test_the_injected_css_leaves_answers_and_chart_labels_alone() -> None:
    assert ".answer-text" not in rd.DEMO_CSS and ".notice" not in rd.DEMO_CSS
    assert "svg.chart" not in rd.DEMO_CSS
    assert f"font-size: {rd.FONT_FLOOR_PX}px !important" in rd.DEMO_CSS
