import json

import pytest
from normalize.probe import MediaReadError, ProbeResult, parse_probe


def _raw(streams: list[dict[str, object]], fmt: dict[str, object] | None = None) -> str:
    return json.dumps({"streams": streams, "format": fmt or {}})


H264: dict[str, object] = {
    "codec_type": "video",
    "codec_name": "h264",
    "width": 1920,
    "height": 1080,
    "avg_frame_rate": "30000/1001",
    "r_frame_rate": "30000/1001",
}
AAC: dict[str, object] = {"codec_type": "audio", "codec_name": "aac"}


def test_reads_video_audio_and_duration() -> None:
    result = parse_probe(_raw([H264, AAC], {"duration": "612.480000"}))

    assert result == ProbeResult(
        has_video=True,
        video_codec="h264",
        duration_sec=612.48,
        avg_fps=pytest.approx(29.97, abs=0.01),  # type: ignore[arg-type]  # 근삿값 비교 (#8)
        base_fps=pytest.approx(29.97, abs=0.01),  # type: ignore[arg-type]  # 근삿값 비교 (#8)
        width=1920,
        height=1080,
        audio_codec="aac",
    )


def test_audio_only_has_no_video() -> None:
    result = parse_probe(_raw([AAC], {"duration": "10.0"}))

    assert not result.has_video
    assert result.video_codec is None


def test_zero_or_missing_rates_and_duration_are_none() -> None:
    stream = {**H264, "avg_frame_rate": "0/0", "r_frame_rate": "0/0"}

    result = parse_probe(_raw([stream], {"duration": "N/A"}))

    assert result.avg_fps is None
    assert result.base_fps is None
    assert result.duration_sec is None


def test_stream_duration_is_used_when_format_has_none() -> None:
    result = parse_probe(_raw([{**H264, "duration": "12.5"}], {}))

    assert result.duration_sec == 12.5


def test_first_video_stream_wins_over_cover_art() -> None:
    cover = {**H264, "codec_name": "mjpeg", "disposition": {"attached_pic": 1}}

    result = parse_probe(_raw([cover, H264], {"duration": "5"}))

    assert result.video_codec == "h264"


def test_r7_unparseable_output_raises_media_read_error() -> None:
    with pytest.raises(MediaReadError):
        parse_probe("not json")


def test_r4_average_rate_is_measured_from_packet_count() -> None:
    # mkv 등은 avg_frame_rate를 기본 프레임률과 같게 적어 가변 프레임률을 숨긴다.
    stream = {**H264, "avg_frame_rate": "30/1", "r_frame_rate": "30/1", "nb_read_packets": "50"}

    result = parse_probe(_raw([stream], {"duration": "3.0"}))

    assert result.avg_fps == pytest.approx(16.667, abs=0.001)
    assert result.base_fps == 30.0


def test_packet_rate_uses_video_stream_duration_when_audio_is_longer() -> None:
    stream = {**H264, "avg_frame_rate": "30/1", "r_frame_rate": "30/1"}
    stream |= {"nb_read_packets": "60", "duration": "2.0"}

    result = parse_probe(_raw([stream, AAC], {"duration": "3.0"}))

    assert result.avg_fps == 30.0
    assert result.duration_sec == 3.0
