"""실제 ffmpeg로 FfmpegTool을 검증한다.

운영과 같은 이미지에서 실행한다(scripts/test-in-docker.sh).
"""

import subprocess
from pathlib import Path

import pytest
from normalize.decision import Normalize
from normalize.ffmpeg import DecodeError, FfmpegTool
from normalize.probe import MediaReadError

pytestmark = pytest.mark.ffmpeg

TOOL = FfmpegTool()


def _make(path: Path, *args: str) -> Path:
    subprocess.run(  # noqa: S603  # 테스트 입력 생성. 인자는 이 파일의 고정값이다 (#8)
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *args, str(path)],  # noqa: S607  # 이미지의 ffmpeg (#8)
        check=True,
    )
    return path


VIDEO = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=30"]
AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440"]


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    return {
        "h264_cfr": _make(
            d / "h264.mp4",
            *VIDEO,
            *AUDIO,
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
        ),
        # 처음 1초는 30fps, 그 뒤는 10fps인 가변 프레임률 영상
        "h264_vfr": _make(
            d / "vfr.mkv",
            *VIDEO,
            "-t",
            "3",
            "-vf",
            "select='lt(t,1)+not(mod(n,3))'",
            "-fps_mode",
            "vfr",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
        ),
        "mpeg4_pcm": _make(
            d / "mpeg4.avi",
            *VIDEO,
            *AUDIO,
            "-t",
            "3",
            "-c:v",
            "mpeg4",
            "-c:a",
            "pcm_s16le",
        ),
        "audio_only": _make(d / "audio.m4a", *AUDIO, "-t", "3", "-c:a", "aac"),
        "half_second": _make(
            d / "short.mp4", *VIDEO, "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p"
        ),
    }


@pytest.fixture
def corrupt(tmp_path: Path) -> Path:
    path = tmp_path / "corrupt.mp4"
    path.write_bytes(b"\x00not a video\xff" * 64)
    return path


def test_r3_h264_cfr_is_copied_with_moov_first(media: dict[str, Path], tmp_path: Path) -> None:
    src = media["h264_cfr"]
    probe = TOOL.probe(src)
    assert probe.video_codec == "h264"
    assert probe.avg_fps == probe.base_fps == 30.0

    out = tmp_path / "video.mp4"
    TOOL.normalize(src, out, Normalize(transcode=False, fps=30.0), probe.audio_codec)

    result = TOOL.probe(out)
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")
    assert result.duration_sec == pytest.approx(3.0, abs=0.1)
    data = out.read_bytes()
    # 재생 정보(moov)가 앞에 있어야 브라우저가 끝까지 받지 않고 재생을 시작한다.
    assert data.index(b"moov") < data.index(b"mdat")


def test_r4_variable_rate_is_detected_and_made_constant(
    media: dict[str, Path], tmp_path: Path
) -> None:
    probe = TOOL.probe(media["h264_vfr"])
    assert probe.avg_fps != pytest.approx(probe.base_fps, rel=0.01)

    out = tmp_path / "video.mp4"
    assert probe.avg_fps is not None
    TOOL.normalize(media["h264_vfr"], out, Normalize(transcode=True, fps=probe.avg_fps), None)

    result = TOOL.probe(out)
    assert result.video_codec == "h264"
    assert result.avg_fps == pytest.approx(result.base_fps, rel=0.01)
    assert result.avg_fps == pytest.approx(probe.avg_fps, rel=0.05)
    assert result.audio_codec is None


def test_r4_other_codec_and_audio_are_reencoded(media: dict[str, Path], tmp_path: Path) -> None:
    probe = TOOL.probe(media["mpeg4_pcm"])
    assert (probe.video_codec, probe.audio_codec) == ("mpeg4", "pcm_s16le")

    out = tmp_path / "video.mp4"
    TOOL.normalize(media["mpeg4_pcm"], out, Normalize(transcode=True, fps=30.0), probe.audio_codec)

    result = TOOL.probe(out)
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")
    assert result.avg_fps == 30.0


def test_r5_hls_playlist_and_segments(media: dict[str, Path], tmp_path: Path) -> None:
    out = tmp_path / "video.mp4"
    TOOL.normalize(media["h264_cfr"], out, Normalize(transcode=False, fps=30.0), "aac")

    files = TOOL.make_hls(out, tmp_path / "hls", segment_s=1.0)

    names = {f.name for f in files}
    assert "index.m3u8" in names
    playlist = (tmp_path / "hls" / "index.m3u8").read_text()
    assert "#EXT-X-ENDLIST" in playlist
    segments = [line for line in playlist.splitlines() if line.endswith(".ts")]
    assert segments
    assert set(segments) <= names
    assert all(f.exists() and f.stat().st_size > 0 for f in files)


def test_r7_corrupt_file_cannot_be_probed(corrupt: Path) -> None:
    with pytest.raises(MediaReadError):
        TOOL.probe(corrupt)


def test_r8_audio_only_has_no_video(media: dict[str, Path]) -> None:
    assert not TOOL.probe(media["audio_only"]).has_video


def test_r10_half_second_duration(media: dict[str, Path]) -> None:
    duration = TOOL.probe(media["half_second"]).duration_sec
    assert duration is not None
    assert duration < 1.0


def test_r12_undecodable_input_raises_decode_error(corrupt: Path, tmp_path: Path) -> None:
    with pytest.raises(DecodeError):
        TOOL.normalize(corrupt, tmp_path / "video.mp4", Normalize(transcode=True, fps=30.0), None)
