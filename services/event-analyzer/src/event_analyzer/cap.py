"""실행당 VLM 호출 상한(SPEC-023 R-19). I/O 없는 규칙만 둔다."""

import math

from vlm_contracts.events import TimeRange

from event_analyzer.profile import CapProfile

_MS_PER_MINUTE = 60_000


def call_cap(owned_range: TimeRange, profile: CapProfile) -> int:
    """실행 하나(소유 범위)의 1차 판정·재판정 호출 수 상한."""
    minutes = (owned_range.end_ms - owned_range.start_ms) / _MS_PER_MINUTE
    return max(profile.floor, math.ceil(profile.per_minute * minutes))
