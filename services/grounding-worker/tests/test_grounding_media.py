"""ffmpeg·ffprobe 호출(SPEC-026 R-2, R-4, R-11, R-13).

`ffmpeg` 마커 테스트는 서비스 이미지 안에서 돌린다(`scripts/test-in-docker.sh grounding-worker`).
"""

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from grounding_worker.detector import Box, Detection
from grounding_worker.media import FfmpegTool, MediaInfo, MediaReadError
from grounding_worker.profile import RenderProfile
from grounding_worker.render import box_windows, filter_script, label_files, target_color

_W, _H = 320, 240


def _make_video(path: Path, *, seconds: int = 4, audio: bool = True) -> Path:
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    args += ["-f", "lavfi", "-i", f"color=c=black:s={_W}x{_H}:r=30:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-shortest", "-movflags", "+faststart"]
    subprocess.run([*args, str(path)], check=True)  # noqa: S603  # 테스트가 만든 인자 목록이다 (#48)
    return path


@pytest.mark.ffmpeg
async def test_spec026_r2_probe_reads_length_fps_size_and_audio(tmp_path: Path) -> None:
    tool = FfmpegTool()

    with_audio = await tool.probe(str(_make_video(tmp_path / "a.mp4")))
    without = await tool.probe(str(_make_video(tmp_path / "b.mp4", audio=False)))

    assert abs(with_audio.duration_ms - 4_000) <= 100
    assert with_audio.fps == pytest.approx(30, abs=0.01)
    assert (with_audio.width, with_audio.height, with_audio.video_codec) == (_W, _H, "h264")
    assert with_audio.has_audio
    assert not without.has_audio


@pytest.mark.ffmpeg
async def test_spec026_r2_extracts_one_rgb_frame_at_the_sample_time(tmp_path: Path) -> None:
    tool = FfmpegTool()
    url = str(_make_video(tmp_path / "a.mp4"))
    info = await tool.probe(url)

    frame = await tool.extract_frame(url, 1_000, info)

    assert (frame.width, frame.height, frame.pts_ms) == (_W, _H, 1_000)
    assert len(frame.data) == _W * _H * 3


@pytest.mark.ffmpeg
async def test_spec026_r11_broken_file_is_unreadable(tmp_path: Path) -> None:
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    with pytest.raises(MediaReadError):
        await FfmpegTool().probe(str(broken))


@pytest.mark.ffmpeg
async def test_spec026_r11_frame_past_the_end_is_unreadable(tmp_path: Path) -> None:
    tool = FfmpegTool()
    url = str(_make_video(tmp_path / "a.mp4", seconds=2))
    info = await tool.probe(url)

    with pytest.raises(MediaReadError):
        await tool.extract_frame(url, 10_000, info)


def _pixel(raw: bytes, x: int, y: int) -> tuple[int, int, int]:
    offset = (y * _W + x) * 3
    r, g, b = raw[offset : offset + 3]
    return r, g, b


@pytest.mark.ffmpeg
async def test_spec026_r4_renders_h264_clip_with_boxes_fps_and_audio(tmp_path: Path) -> None:
    """[SPEC-026 R-4] 구간 길이·원본 fps의 H.264 클립에 대상어 색 박스가 그려지고 오디오가 남는다.

    박스 색은 대상어 색이다(R-5).
    """
    tool = FfmpegTool()
    url = str(_make_video(tmp_path / "src.mp4"))
    info = await tool.probe(url)
    profile = RenderProfile(line_width=8)
    work = tmp_path / "work"
    work.mkdir()
    target = "forklift"
    detection = Detection(target, Box(0.25, 0.25, 0.5, 0.5), 0.9)
    script = work / "filter.txt"
    script.write_text(
        filter_script(
            [[detection], []],
            box_windows([1_000, 2_000], start_ms=1_000),
            width=info.width,
            height=info.height,
            labels=label_files([target], work),
            profile=profile,
        ),
        encoding="utf-8",
    )
    clip = work / "clip.mp4"

    await tool.render_clip(
        url, start_ms=1_000, duration_ms=2_000, script=script, dst=clip, info=info, profile=profile
    )

    out = await tool.probe(str(clip))
    assert out.video_codec == "h264"
    assert out.fps == pytest.approx(30, abs=0.01)
    assert abs(out.duration_ms - 2_000) <= 100
    assert out.has_audio
    # 첫 샘플(0~0.5초)에는 박스가 있고, 두 번째 샘플(탐지 없음) 구간에는 없다.
    with_box = await tool.extract_frame(str(clip), 200, out)
    without_box = await tool.extract_frame(str(clip), 1_500, out)
    expected = bytes.fromhex(target_color(target)[1:])
    left_edge = _pixel(with_box.data, 80 + 3, 120)
    assert all(abs(a - b) <= 40 for a, b in zip(left_edge, expected, strict=True)), left_edge
    assert max(_pixel(with_box.data, 160, 120)) <= 30
    assert max(_pixel(without_box.data, 80 + 3, 120)) <= 30


async def test_spec026_r13_cancelling_kills_the_process(tmp_path: Path) -> None:
    """[SPEC-026 R-13] 제한 시간으로 취소되면 ffmpeg 프로세스를 남기지 않는다."""
    pid_file = tmp_path / "pid"
    fake = tmp_path / "fake-ffprobe"
    fake.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 30\n", encoding="utf-8")
    fake.chmod(0o755)
    tool = FfmpegTool(ffprobe=str(fake))

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(1):
            await tool.probe("memory://x")

    pid = int(pid_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_media_info_frame_interval() -> None:
    info = MediaInfo(
        duration_ms=10_000, fps=25, width=_W, height=_H, video_codec="h264", has_audio=False
    )

    assert info.frame_interval_ms == 40
