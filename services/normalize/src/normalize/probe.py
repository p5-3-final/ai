"""ffprobe 출력(JSON) 해석."""

import json
from dataclasses import dataclass
from fractions import Fraction

from vlm_common.errors import VlmError


class MediaReadError(VlmError):
    """원본을 열 수 없다(SPEC-007 R-7 `UNREADABLE`)."""

    code = "NORMALIZE_UNREADABLE"


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """판정에 필요한 원본 정보.

    Attributes:
        avg_fps: 평균 프레임률. 패킷 수를 셌으면 패킷 수 ÷ 길이, 아니면 `avg_frame_rate`.
        base_fps: 기본 프레임률(`r_frame_rate`). 평균과 다르면 가변 프레임률로 본다.
    """

    has_video: bool
    video_codec: str | None
    duration_sec: float | None
    avg_fps: float | None
    base_fps: float | None
    width: int | None
    height: int | None
    audio_codec: str | None


def parse_probe(raw: str) -> ProbeResult:
    """`ffprobe -show_streams -show_format -of json` 출력을 해석한다.

    Raises:
        MediaReadError: 출력이 JSON이 아닐 때.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise MediaReadError("ffprobe output is not JSON") from e
    streams: list[dict[str, object]] = data.get("streams", [])
    fmt: dict[str, object] = data.get("format", {})
    # 음악 파일 표지 같은 첨부 그림은 영상 스트림으로 치지 않는다.
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not _is_attached_picture(s)),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _positive_float(fmt.get("duration"))
    if duration is None and video is not None:
        duration = _positive_float(video.get("duration"))
    return ProbeResult(
        has_video=video is not None,
        video_codec=_str(video.get("codec_name")) if video else None,
        duration_sec=duration,
        avg_fps=_average_rate(video, fallback_duration=duration) if video else None,
        base_fps=_rate(video.get("r_frame_rate")) if video else None,
        width=_int(video.get("width")) if video else None,
        height=_int(video.get("height")) if video else None,
        audio_codec=_str(audio.get("codec_name")) if audio else None,
    )


def _average_rate(video: dict[str, object], fallback_duration: float | None) -> float | None:
    # mkv 등은 avg_frame_rate를 기본 프레임률과 같게 적어 가변 프레임률을 숨긴다.
    # `-count_packets`로 센 패킷 수가 있으면 실제 평균으로 계산한다(SPEC-007 R-4).
    # 음성이 더 길면 전체 길이로 나눈 값이 낮아지므로 영상 스트림 길이를 먼저 쓴다.
    duration = _positive_float(video.get("duration")) or fallback_duration
    packets = _positive_float(video.get("nb_read_packets"))
    if packets is not None and duration is not None:
        return packets / duration
    return _rate(video.get("avg_frame_rate"))


def _is_attached_picture(stream: dict[str, object]) -> bool:
    disposition = stream.get("disposition")
    return isinstance(disposition, dict) and disposition.get("attached_pic") == 1


def _rate(value: object) -> float | None:
    # ffprobe는 프레임률을 "30000/1001"처럼 분수로, 모르면 "0/0"으로 준다.
    if not isinstance(value, str):
        return None
    try:
        rate = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return float(rate) if rate > 0 else None


def _positive_float(value: object) -> float | None:
    try:
        number = float(str(value))
    except ValueError:
        return None
    return number if number > 0 else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None
