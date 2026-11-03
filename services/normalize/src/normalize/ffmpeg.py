"""ffprobe·ffmpeg 호출. 서비스 코드는 `MediaTool` 프로토콜만 쓴다(ADR-0008 교체 원칙)."""

import subprocess
from pathlib import Path
from typing import Protocol

from vlm_common.errors import VlmError

from normalize.decision import Normalize
from normalize.probe import MediaReadError, ProbeResult, parse_probe

# 오류 메시지에 남길 stderr 끝부분 길이. 긴 ffmpeg 로그 전체를 예외에 담지 않는다.
_STDERR_TAIL = 500


class DecodeError(VlmError):
    """원본을 디코딩하거나 결과를 만들지 못했다(SPEC-007 R-12 `DECODE_FAILED`)."""

    code = "NORMALIZE_DECODE_FAILED"


class MediaTool(Protocol):
    """영상 검사와 변환. 블로킹 호출이다."""

    def probe(self, path: Path) -> ProbeResult:
        """파일 정보를 읽는다. 열 수 없으면 `MediaReadError`."""
        ...

    def normalize(self, src: Path, dst: Path, plan: Normalize, audio_codec: str | None) -> None:
        """정규화 사본을 만든다. 실패하면 `DecodeError`."""
        ...

    def make_hls(self, src: Path, out_dir: Path, segment_s: float) -> list[Path]:
        """`src`를 HLS로 잘라 `out_dir`에 쓰고 만든 파일을 돌려준다. 실패하면 `DecodeError`."""
        ...


class FfmpegTool:
    """시스템 ffmpeg·ffprobe 구현. 인자는 목록으로 넘겨 셸을 거치지 않는다."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    def probe(self, path: Path) -> ProbeResult:
        # 패킷만 세고 디코딩은 하지 않아 길이에 비해 빠르다.
        args = [self._ffprobe, "-v", "error", "-count_packets", "-show_streams", "-show_format"]
        result = _run([*args, "-of", "json", str(path)])
        if result.returncode != 0:
            raise MediaReadError(f"ffprobe failed: {_tail(result.stderr)}")
        return parse_probe(result.stdout)

    def normalize(self, src: Path, dst: Path, plan: Normalize, audio_codec: str | None) -> None:
        if plan.transcode:
            # 프레임 시각이 일정해야 이후 단계의 PTS 계산이 맞는다(SPEC-007 R-4).
            video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
            video += ["-r", f"{plan.fps:.6g}", "-fps_mode", "cfr"]
        else:
            video = ["-c:v", "copy"]
        audio = ["-c:a", "copy"] if audio_codec == "aac" else ["-c:a", "aac", "-b:a", "128k"]
        args = [self._ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src)]
        # 첫 영상·음성 스트림만 남긴다. 음성이 없어도 실패하지 않게 `?`를 붙인다.
        args += ["-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn", *video, *audio]
        args += ["-movflags", "+faststart", "-f", "mp4", str(dst)]
        _check(_run(args))

    def make_hls(self, src: Path, out_dir: Path, segment_s: float) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        args = [self._ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(src), "-c", "copy"]
        args += ["-f", "hls", "-hls_time", f"{segment_s:g}", "-hls_playlist_type", "vod"]
        args += ["-hls_segment_filename", str(out_dir / "seg_%05d.ts"), str(out_dir / "index.m3u8")]
        _check(_run(args))
        return sorted(out_dir.iterdir())


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603  # 인자는 코드가 만든 목록이고 셸을 쓰지 않는다 (#8)


def _check(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        raise DecodeError(f"ffmpeg failed: {_tail(result.stderr)}")


def _tail(stderr: str) -> str:
    return stderr.strip()[-_STDERR_TAIL:]
