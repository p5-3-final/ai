"""원본을 거절할지, 복사할지, 다시 인코딩할지 정한다(SPEC-007 R-3, R-4, R-8~R-11)."""

from dataclasses import dataclass

from vlm_contracts.events import RejectReason

from normalize.probe import ProbeResult

# 평균·기본 프레임률이 이 비율 안에서 같으면 고정 프레임률로 본다. 분수 반올림 차이를 흡수한다.
_CONSTANT_RATE_TOLERANCE = 0.01


@dataclass(frozen=True, slots=True)
class Limits:
    """설정 값으로 바꿀 수 있는 기준(SPEC-007 용어)."""

    min_duration_s: float
    max_duration_s: float
    max_fps: float


@dataclass(frozen=True, slots=True)
class Reject:
    reason: RejectReason


@dataclass(frozen=True, slots=True)
class Normalize:
    """정규화 방식. `fps`는 결과 사본의 프레임률이다."""

    transcode: bool
    fps: float


def decide(probe: ProbeResult, limits: Limits) -> Reject | Normalize:
    """원본 정보로 정규화 방식을 정한다. 거절 조건은 R-8부터 순서대로 본다."""
    if not probe.has_video:
        return Reject("NO_VIDEO_STREAM")
    if probe.duration_sec is None or probe.duration_sec <= 0:
        return Reject("MISSING_METADATA")
    if probe.avg_fps is None or probe.avg_fps <= 0:
        return Reject("MISSING_METADATA")
    if probe.duration_sec < limits.min_duration_s:
        return Reject("TOO_SHORT")
    if probe.duration_sec > limits.max_duration_s:
        return Reject("TOO_LONG")
    if probe.video_codec == "h264" and _is_constant_rate(probe):
        return Normalize(transcode=False, fps=probe.avg_fps)
    return Normalize(transcode=True, fps=min(probe.avg_fps, limits.max_fps))


def _is_constant_rate(probe: ProbeResult) -> bool:
    if probe.avg_fps is None or probe.base_fps is None:
        return False
    return abs(probe.avg_fps - probe.base_fps) <= probe.base_fps * _CONSTANT_RATE_TOLERANCE
