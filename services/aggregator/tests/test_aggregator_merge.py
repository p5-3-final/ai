"""청크 경계 이벤트 병합(SPEC-027 R-6, R-7)."""

from agg_support import CHUNK_A, CHUNK_B, HAZARD, JOB, event, uid
from aggregator.merge import merge_events

CHUNK_C = uid(0xC)
GAP = 10_000


def test_spec027_r6_same_track_and_type_across_chunks_within_gap_merge() -> None:
    """[SPEC-027 R-6] 같은 유형·같은 트랙·다른 청크, 간격 5초면 한 묶음이다."""
    a = event(1, start=50_000, end=55_000, chunk=CHUNK_A, track=uid(100))
    b = event(2, start=60_000, end=65_000, chunk=CHUNK_B, track=uid(100))

    merged = merge_events([a, b], gap_ms=GAP, job_id=JOB)

    [group] = merged.groups
    assert (group.start_ms, group.end_ms) == (50_000, 65_000)
    assert len(merged.representatives) == 1


def test_spec027_r6_gap_exactly_at_limit_merges_and_beyond_does_not() -> None:
    at_limit = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100)),
        event(2, start=15_000, end=20_000, chunk=CHUNK_B, track=uid(100)),
    ]
    beyond = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100)),
        event(2, start=15_001, end=20_000, chunk=CHUNK_B, track=uid(100)),
    ]

    assert len(merge_events(at_limit, gap_ms=GAP, job_id=JOB).groups) == 1
    assert merge_events(beyond, gap_ms=GAP, job_id=JOB).groups == ()


def test_spec027_r6_chain_merges_into_one_group() -> None:
    """[SPEC-027 R-6] 이어진 이벤트는 모두 같은 묶음이다(A-B, B-C면 A·B·C)."""
    events = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100)),
        event(2, start=10_000, end=15_000, chunk=CHUNK_B, track=uid(100)),
        event(3, start=20_000, end=25_000, chunk=CHUNK_C, track=uid(100)),
    ]

    [group] = merge_events(events, gap_ms=GAP, job_id=JOB).groups

    assert len(group.merged_event_ids) == 2


def test_spec027_r6_same_chunk_or_missing_chunk_is_not_merged() -> None:
    same_chunk = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100)),
        event(2, start=6_000, end=8_000, chunk=CHUNK_A, track=uid(100)),
    ]
    no_chunk = [
        event(1, start=0, end=5_000, chunk=None, track=uid(100)),
        event(2, start=6_000, end=8_000, chunk=CHUNK_B, track=uid(100)),
    ]

    assert merge_events(same_chunk, gap_ms=GAP, job_id=JOB).groups == ()
    assert merge_events(no_chunk, gap_ms=GAP, job_id=JOB).groups == ()


def test_spec027_r6_without_tracks_the_zone_is_the_key() -> None:
    """[SPEC-027 R-6] 둘 다 trackId가 없으면 같은 zoneId일 때만 잇는다. 다른 유형은 잇지 않는다."""
    zoned = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, zone=HAZARD),
        event(2, start=6_000, end=8_000, chunk=CHUNK_B, zone=HAZARD),
    ]
    other_type = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, zone=HAZARD),
        event(2, start=6_000, end=8_000, chunk=CHUNK_B, zone=HAZARD, event_type="fall"),
    ]

    assert len(merge_events(zoned, gap_ms=GAP, job_id=JOB).groups) == 1
    assert merge_events(other_type, gap_ms=GAP, job_id=JOB).groups == ()


def test_spec027_r7_highest_score_is_kept_then_earliest_then_event_id() -> None:
    """[SPEC-027 R-7] 대표는 점수 최고(null은 최저) → 이른 startMs → eventId 순이다."""
    events = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100), score=None),
        event(2, start=6_000, end=8_000, chunk=CHUNK_B, track=uid(100), score=0.4),
        event(3, start=9_000, end=12_000, chunk=CHUNK_C, track=uid(100), score=0.9),
    ]
    ties = [
        event(5, start=6_000, end=8_000, chunk=CHUNK_B, track=uid(100), score=0.4),
        event(4, start=6_000, end=8_000, chunk=CHUNK_A, track=uid(100), score=0.4),
    ]

    [group] = merge_events(events, gap_ms=GAP, job_id=JOB).groups
    [tie] = merge_events(ties, gap_ms=GAP, job_id=JOB).groups

    assert group.kept_event_id == uid(3)
    assert group.merged_event_ids == [uid(1), uid(2)]
    assert tie.kept_event_id == uid(4)


def test_spec027_r7_non_representatives_are_dropped_and_singles_kept() -> None:
    """[SPEC-027 R-7] 대표가 아닌 이벤트만 빠지고, 묶이지 않은 이벤트는 그대로 남는다."""
    events = [
        event(1, start=0, end=5_000, chunk=CHUNK_A, track=uid(100), score=0.9),
        event(2, start=6_000, end=8_000, chunk=CHUNK_B, track=uid(100), score=0.1),
        event(3, start=200_000, chunk=CHUNK_B, track=uid(101)),
    ]

    merged = merge_events(events, gap_ms=GAP, job_id=JOB)

    assert [e.event_id for e in merged.representatives] == [uid(1), uid(3)]
