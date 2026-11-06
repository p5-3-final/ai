"""소유 범위가 영상 전체를 덮는지(SPEC-022 R-8, R-15)."""

from collections.abc import Iterable

from vlm_contracts.events import TimeRange


def missing_ranges(ranges: Iterable[TimeRange], duration_ms: int) -> list[TimeRange]:
    """`[0, duration_ms)` 중 어느 범위에도 들지 않는 구간(시간순). 영상 밖 부분은 자른다."""
    missing: list[TimeRange] = []
    cursor = 0
    for r in sorted(ranges, key=lambda r: (r.start_ms, r.end_ms)):
        if r.start_ms >= duration_ms:
            break
        if r.start_ms > cursor:
            missing.append(TimeRange(start_ms=cursor, end_ms=r.start_ms))
        cursor = max(cursor, r.end_ms)
    if cursor < duration_ms:
        missing.append(TimeRange(start_ms=cursor, end_ms=duration_ms))
    return missing


def covers(ranges: Iterable[TimeRange], duration_ms: int) -> bool:
    """소유 범위 합집합이 `[0, duration_ms)`를 빈틈없이 덮으면 True."""
    return not missing_ranges(ranges, duration_ms)
