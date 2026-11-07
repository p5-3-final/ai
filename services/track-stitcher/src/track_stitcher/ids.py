"""결정적 ID(SPEC-022 R-5, R-12, R-14).

같은 입력이면 어느 Pod가 몇 번 처리해도 같은 ID가 나와 소비자가 eventId로 중복을 흡수한다
(NFR-2). event-analyzer `ids.py`와 같은 uuid5 방식이다.
"""

from collections.abc import Iterable
from uuid import NAMESPACE_URL, UUID, uuid5

from track_stitcher.model import LocalTrack, TrackRef


def earliest(tracks: Iterable[LocalTrack]) -> LocalTrack:
    """가장 이른 구성원(시작 시각, 같으면 chunkId·trackLocalId 순, R-14)."""
    return min(tracks, key=LocalTrack.order_key)


def final_track_id(job_id: UUID, member: TrackRef) -> UUID:
    """최종 trackId. jobId와 가장 이른 구성원으로 정한다(R-14)."""
    return uuid5(NAMESPACE_URL, f"vlm:track:{job_id}:{member.chunk_id}:{member.track_local_id}")


def track_detected_event_id(job_id: UUID, earlier_chunk: UUID, later_chunk: UUID) -> UUID:
    """경계 한 쌍의 `track.detected`(origin stitcher) eventId(R-5)."""
    return uuid5(NAMESPACE_URL, f"vlm:track.detected:{job_id}:{earlier_chunk}:{later_chunk}")


def track_manifest_event_id(job_id: UUID) -> UUID:
    """Job의 `track.manifest` eventId(R-12)."""
    return uuid5(NAMESPACE_URL, f"vlm:track.manifest:{job_id}")
