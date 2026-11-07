"""소유 범위 커버리지(SPEC-022 R-8, R-15)."""

from track_stitcher.coverage import covers, missing_ranges
from vlm_contracts.events import TimeRange


def _r(start: int, end: int) -> TimeRange:
    return TimeRange(start_ms=start, end_ms=end)


def test_spec022_r8_ranges_covering_whole_video_in_any_order() -> None:
    ranges = [_r(200, 300), _r(0, 100), _r(100, 200)]

    assert covers(ranges, 300)
    assert missing_ranges(ranges, 300) == []


def test_spec022_r15_gaps_head_and_tail_are_missing() -> None:
    ranges = [_r(100, 200), _r(300, 400)]

    assert not covers(ranges, 500)
    assert missing_ranges(ranges, 500) == [_r(0, 100), _r(200, 300), _r(400, 500)]


def test_spec022_r15_no_chunks_is_whole_video_missing() -> None:
    assert missing_ranges([], 1000) == [_r(0, 1000)]


def test_spec022_r8_overlapping_and_out_of_range_parts_are_clipped() -> None:
    ranges = [_r(0, 150), _r(100, 250), _r(250, 900)]

    assert covers(ranges, 400)
    assert missing_ranges([_r(500, 900)], 400) == [_r(0, 400)]
