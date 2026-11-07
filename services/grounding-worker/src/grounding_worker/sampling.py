"""샘플 시각 계산(SPEC-026 R-2)."""


def sample_times(start_ms: int, end_ms: int, *, interval_ms: int, max_samples: int) -> list[int]:
    """구간 `[start_ms, end_ms)`에서 `start_ms`부터 `interval_ms`마다, 최대 `max_samples`개.

    샘플 수 상한(12)은 탐지 비용을 구간 길이와 상관없이 묶어 두려는 값이다(spec 1절 메모리 표).
    """
    times: list[int] = []
    at = start_ms
    while at < end_ms and len(times) < max_samples:
        times.append(at)
        at += interval_ms
    return times
