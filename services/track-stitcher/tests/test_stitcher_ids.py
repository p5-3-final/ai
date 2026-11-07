"""결정적 ID(SPEC-022 R-5, R-12, R-14)."""

from uuid import UUID

from stitcher_support import JOB, chunk_tracks, track
from track_stitcher.ids import (
    earliest,
    final_track_id,
    track_detected_event_id,
    track_manifest_event_id,
)
from track_stitcher.model import local_tracks


def test_spec022_r14_earliest_member_by_start_then_chunk_then_local_id() -> None:
    a = local_tracks(chunk_tracks(0, [track("z", 100, 200), track("b", 50, 300)]))
    b = local_tracks(chunk_tracks(1, [track("a", 50, 300)]))

    first = earliest([*b, *a])

    # 시작 시각이 같으면 chunkId, 그다음 trackLocalId 순이다. 청크 0의 ID가 더 작다.
    assert (first.ref.chunk_id, first.ref.track_local_id) == (a[1].ref.chunk_id, "b")


def test_spec022_r14_final_track_id_depends_on_job_and_member_only() -> None:
    [member] = local_tracks(chunk_tracks(0, [track("t1", 0, 10)]))
    other_job = UUID(int=JOB.int + 1)

    assert final_track_id(JOB, member.ref) == final_track_id(JOB, member.ref)
    assert final_track_id(JOB, member.ref) != final_track_id(other_job, member.ref)
    assert final_track_id(JOB, member.ref).version == 5


def test_spec022_r5_r12_event_ids_are_deterministic_and_distinct() -> None:
    a, b = UUID(int=1), UUID(int=2)

    assert track_detected_event_id(JOB, a, b) == track_detected_event_id(JOB, a, b)
    assert track_detected_event_id(JOB, a, b) != track_detected_event_id(JOB, b, a)
    assert track_manifest_event_id(JOB) == track_manifest_event_id(JOB)
    assert track_manifest_event_id(JOB) != track_detected_event_id(JOB, a, b)
