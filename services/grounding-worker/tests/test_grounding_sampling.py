"""샘플 시각(SPEC-026 R-2)."""

import pytest
from grounding_worker.sampling import sample_times


def test_spec026_r2_samples_every_interval_from_start() -> None:
    assert sample_times(40_000, 50_000, interval_ms=1_000, max_samples=12) == [
        40_000 + k * 1_000 for k in range(10)
    ]


def test_spec026_r2_end_is_exclusive() -> None:
    assert sample_times(0, 3_000, interval_ms=1_000, max_samples=12) == [0, 1_000, 2_000]
    assert sample_times(0, 3_001, interval_ms=1_000, max_samples=12) == [0, 1_000, 2_000, 3_000]


def test_spec026_r2_at_most_max_samples() -> None:
    times = sample_times(0, 60_000, interval_ms=1_000, max_samples=12)

    assert times == [k * 1_000 for k in range(12)]


def test_spec026_r2_short_segment_has_one_sample() -> None:
    assert sample_times(5_000, 5_400, interval_ms=1_000, max_samples=12) == [5_000]


def test_spec026_r2_interval_is_configurable() -> None:
    assert sample_times(0, 1_000, interval_ms=250, max_samples=12) == [0, 250, 500, 750]


@pytest.mark.parametrize(("start", "end"), [(1_000, 1_000), (2_000, 1_000)])
def test_empty_segment_has_no_samples(start: int, end: int) -> None:
    assert sample_times(start, end, interval_ms=1_000, max_samples=12) == []
