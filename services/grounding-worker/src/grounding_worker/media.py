"""ffprobe·ffmpeg 호출. 서비스 코드는 `MediaTool` 프로토콜만 쓴다(ADR-0008 교체 원칙).

입력은 저장소 서명 주소(또는 로컬 경로)다. 정규화 사본은 `+faststart`라 ffmpeg가 HTTP 범위 요청으로
구간만 읽는다(plan 4절). 프로세스는 `asyncio`로 띄워, 구간 제한 시간으로 취소되면 종료한다(R-13).
"""

import asyncio
import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Protocol

from vlm_common.errors import VlmError

from grounding_worker.detector import RgbFrame
from grounding_worker.profile import RenderProfile

# 오류 메시지에 남길 stderr 끝부분 길이. 서명 주소가 든 긴 ffmpeg 로그 전체를 예외에 담지 않는다.
_STDERR_TAIL = 300


class MediaReadError(VlmError):
    """원본을 열거나 디코딩하지 못했다(SPEC-026 R-11)."""

    code = "GROUNDING_SOURCE_UNREADABLE"


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """영상 정보. 클립 결과(`clip`)도 이 값으로 채운다."""

    duration_ms: int
    fps: float
    width: int
    height: int
    video_codec: str
    has_audio: bool

    @property
    def frame_interval_ms(self) -> float:
        """프레임 하나의 길이. 구간 끝과 영상 길이 비교의 허용 오차다(보고 C-10)."""
        return 1000 / self.fps


class MediaTool(Protocol):
    """영상 검사·샘플 추출·클립 렌더링."""

    async def probe(self, url: str) -> MediaInfo:
        """정보를 읽는다. 열 수 없으면 `MediaReadError`."""
        ...

    async def extract_frame(self, url: str, pts_ms: int, info: MediaInfo) -> RgbFrame:
        """`pts_ms`의 프레임 하나를 rgb24로 뽑는다. 실패하면 `MediaReadError`."""
        ...

    async def render_clip(
        self,
        url: str,
        *,
        start_ms: int,
        duration_ms: int,
        script: Path,
        dst: Path,
        info: MediaInfo,
        profile: RenderProfile,
    ) -> None:
        """구간을 필터 스크립트로 그려 H.264 mp4로 쓴다. 실패하면 `MediaReadError`."""
        ...


class FfmpegTool:
    """시스템 ffmpeg·ffprobe 구현. 인자는 목록으로 넘겨 셸을 거치지 않는다."""

    def __init__(self, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> None:
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

    async def probe(self, url: str) -> MediaInfo:
        args = [self._ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", url]
        code, out, err = await _run(args)
        if code != 0:
            raise MediaReadError(f"ffprobe failed: {_tail(err)}")
        return _parse_probe(out)

    async def extract_frame(self, url: str, pts_ms: int, info: MediaInfo) -> RgbFrame:
        # 입력 앞의 -ss는 키프레임으로 건너뛴 뒤 그 시각까지 디코딩한다(정확한 탐색). 프레임마다
        # 프로세스 하나라 메모리는 프레임 한 장 단위다.
        args = [self._ffmpeg, "-nostdin", "-v", "error", "-ss", _seconds(pts_ms), "-i", url]
        args += ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        code, out, err = await _run(args)
        if code != 0 or len(out) != info.width * info.height * 3:
            raise MediaReadError(f"frame extraction failed at {pts_ms}ms: {_tail(err)}")
        return RgbFrame(data=out, width=info.width, height=info.height, pts_ms=pts_ms)

    async def render_clip(
        self,
        url: str,
        *,
        start_ms: int,
        duration_ms: int,
        script: Path,
        dst: Path,
        info: MediaInfo,
        profile: RenderProfile,
    ) -> None:
        args = [self._ffmpeg, "-nostdin", "-v", "error", "-y"]
        args += ["-ss", _seconds(start_ms), "-t", _seconds(duration_ms), "-i", url]
        # `-/filter:v`는 필터 그래프를 파일에서 읽는다(ffmpeg 7). 명령행 길이와 인자 해석을 피한다.
        args += ["-/filter:v", str(script), "-map", "0:v:0"]
        # SPEC-026 R-4: 원본 fps 그대로(프레임 복제·삭제 없음) 다시 인코딩한다.
        args += ["-c:v", "libx264", "-preset", profile.preset, "-crf", str(profile.crf)]
        args += ["-pix_fmt", "yuv420p", "-fps_mode", "passthrough"]
        if info.has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        args += ["-sn", "-dn", "-movflags", "+faststart", "-f", "mp4", str(dst)]
        code, _, err = await _run(args)
        if code != 0:
            raise MediaReadError(f"clip rendering failed: {_tail(err)}")


async def _run(args: list[str]) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await process.communicate()
    finally:
        # 취소(R-13 제한 시간)나 오류로 끝나도 프로세스를 남기지 않는다.
        if process.returncode is None:
            process.kill()
            await process.wait()
    return process.returncode or 0, out, err


def _parse_probe(raw: bytes) -> MediaInfo:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MediaReadError("ffprobe output is not JSON") from e
    streams: list[dict[str, object]] = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None:
        raise MediaReadError("no video stream")
    duration = _positive(data.get("format", {}).get("duration")) or _positive(video.get("duration"))
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    width, height, codec = video.get("width"), video.get("height"), video.get("codec_name")
    if (
        duration is None
        or fps is None
        or not isinstance(width, int)
        or not isinstance(height, int)
        or not isinstance(codec, str)
        or width < 1
        or height < 1
    ):
        raise MediaReadError("missing duration, fps or frame size")
    return MediaInfo(
        duration_ms=round(duration * 1000),
        fps=fps,
        width=width,
        height=height,
        video_codec=codec,
        has_audio=has_audio,
    )


def _rate(value: object) -> float | None:
    # ffprobe는 프레임률을 "30000/1001"처럼 분수로, 모르면 "0/0"으로 준다.
    if not isinstance(value, str):
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate > 0 else None


def _positive(value: object) -> float | None:
    try:
        number = float(str(value))
    except ValueError:
        return None
    return number if number > 0 else None


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.3f}"


def _tail(stderr: bytes) -> str:
    return stderr.decode("utf-8", "replace")[-_STDERR_TAIL:].strip()
