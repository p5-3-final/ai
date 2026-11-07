"""연결 계산이 쓰는 내부 값 객체."""

from collections.abc import Iterable, Sequence
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


def connected(
    tracks: Sequence[LocalTrack], edges: Iterable[tuple[TrackRef, TrackRef]]
) -> list[tuple[LocalTrack, ...]]:
    """연결로 묶인 트랙 묶음(union-find). 묶음 안과 묶음 사이 모두 R-14 순서라 결정적이다.

    `tracks`에 없는 트랙을 가리키는 연결은 무시한다.
    """
    parent = {t.ref: t.ref for t in tracks}

    def root(ref: TrackRef) -> TrackRef:
        while parent[ref] != ref:
            parent[ref] = parent[parent[ref]]
            ref = parent[ref]
        return ref

    for a, b in edges:
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    groups: dict[TrackRef, list[LocalTrack]] = {}
    for t in tracks:
        groups.setdefault(root(t.ref), []).append(t)
    ordered = [tuple(sorted(g, key=LocalTrack.order_key)) for g in groups.values()]
    return sorted(ordered, key=lambda g: g[0].order_key())
