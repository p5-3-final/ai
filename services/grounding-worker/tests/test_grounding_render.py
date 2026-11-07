"""대상어 색, 샘플별 표시 구간, ffmpeg 필터 스크립트(SPEC-026 R-4, R-5)."""

import hashlib
from pathlib import Path

import pytest
from grounding_worker.detector import Box, Detection
from grounding_worker.profile import RenderProfile
from grounding_worker.render import Window, box_windows, filter_script, label_files, target_color


def test_spec026_r5_color_is_first_three_sha256_bytes() -> None:
    expected = "#" + hashlib.sha256(b"forklift").hexdigest()[:6]

    assert target_color("forklift") == expected


def test_spec026_r5_same_target_same_color_regardless_of_request() -> None:
    assert target_color("person without a hard hat") == target_color("person without a hard hat")
    assert target_color("forklift") != target_color("forklifts")


def test_spec026_r4_window_boundaries_are_midpoints_between_samples() -> None:
    windows = box_windows([40_000, 41_000, 42_000], start_ms=40_000)

    assert windows == [Window(None, 0.5), Window(0.5, 1.5), Window(1.5, None)]


def test_spec026_r4_single_sample_covers_whole_clip() -> None:
    assert box_windows([40_000], start_ms=40_000) == [Window(None, None)]


def test_spec026_r4_window_enable_expression_is_half_open() -> None:
    assert Window(None, 0.5).enable() == "lt(t,0.5)"
    assert Window(0.5, 1.5).enable() == "gte(t,0.5)*lt(t,1.5)"
    assert Window(1.5, None).enable() == "gte(t,1.5)"
    assert Window(None, None).enable() == "1"


def _profile() -> RenderProfile:
    return RenderProfile(font_path=Path("/fonts/DejaVuSans.ttf"), line_width=3, font_size=24)


def test_spec026_r4_no_detection_draws_nothing(tmp_path: Path) -> None:
    script = filter_script(
        [[], []],
        box_windows([0, 1_000], 0),
        width=1920,
        height=1080,
        labels={},
        profile=_profile(),
    )

    assert script == "null"


def test_spec026_r4_r5_boxes_use_target_color_and_window(tmp_path: Path) -> None:
    labels = label_files(["forklift"], tmp_path)
    detection = Detection("forklift", Box(0.5, 0.25, 0.25, 0.5), 0.9)

    script = filter_script(
        [[detection], []],
        box_windows([0, 1_000], 0),
        width=1920,
        height=1080,
        labels=labels,
        profile=_profile(),
    )

    color = target_color("forklift")[1:]
    assert f"drawbox=x=960:y=270:w=480:h=540:color=0x{color}:t=3:enable='lt(t,0.5)'" in script
    assert f"textfile={labels['forklift']}" in script
    assert "fontfile=/fonts/DejaVuSans.ttf" in script
    assert "expansion=none" in script
    assert script.count("drawbox") == 1


def test_spec026_r4_label_text_goes_to_files_not_the_filter(tmp_path: Path) -> None:
    """대상어(`'` 포함)는 필터 문자열에 넣지 않고 파일로 넘겨 필터 문법 주입을 막는다."""
    target = "worker's hard hat"
    labels = label_files([target], tmp_path)
    detection = Detection(target, Box(0, 0, 0.1, 0.1), 0.9)

    script = filter_script(
        [[detection]],
        box_windows([0], 0),
        width=640,
        height=480,
        labels=labels,
        profile=_profile(),
    )

    assert target not in script
    assert labels[target].read_text(encoding="utf-8") == target


def test_label_files_reject_unsafe_folder(tmp_path: Path) -> None:
    unsafe = tmp_path / "a:b"
    unsafe.mkdir()

    with pytest.raises(ValueError, match="path"):
        label_files(["forklift"], unsafe)


def test_spec026_r4_label_sits_above_the_box_inside_the_frame(tmp_path: Path) -> None:
    labels = label_files(["forklift"], tmp_path)
    detection = Detection("forklift", Box(0.1, 0.0, 0.2, 0.2), 0.9)

    script = filter_script(
        [[detection]],
        box_windows([0], 0),
        width=1000,
        height=1000,
        labels=labels,
        profile=_profile(),
    )

    assert ":x=100:y=0:" in script
