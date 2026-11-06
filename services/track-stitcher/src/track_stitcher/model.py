"""연결 계산이 쓰는 내부 값 객체."""

from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.events import TrackMember
from vlm_contracts.tracks import ChunkTracks


@dataclass(frozen=True, slots=True)
class TrackRef:
    """로컬 트랙 하나를 가리키는 키. trackLocalId는 실행 안에서만 고유하다."""

    chunk_id: UUID
    execution_id: UUID
    track_local_id: str

    def member(self) -> TrackMember:
        return TrackMember(
            chunk_id=self.chunk_id,
            execution_id=self.execution_id,
            track_local_id=self.track_local_id,
        )


@dataclass(frozen=True, slots=True)
class LocalTrack:
    """청크 실행 하나의 로컬 트랙(경계 박스 제외)."""

    ref: TrackRef
    class_name: str
    start_ms: int
    end_ms: int

    def order_key(self) -> tuple[int, str, str]:
        """R-14의 "가장 이른 구성원" 순서: 시작 시각, chunkId, trackLocalId."""
        return (self.start_ms, str(self.ref.chunk_id), self.ref.track_local_id)


def local_tracks(file: ChunkTracks) -> list[LocalTrack]:
    """트랙 요약 파일의 로컬 트랙 목록(파일 순서)."""
    return [
        LocalTrack(
            ref=TrackRef(file.chunk_id, file.execution_id, t.track_local_id),
            class_name=t.class_name,
            start_ms=t.start_ms,
            end_ms=t.end_ms,
        )
        for t in file.tracks
    ]
