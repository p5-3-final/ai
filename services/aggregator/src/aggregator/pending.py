"""Kafka로 받은 판정·트랙을 모아 두는 대기 집합(R-25, R-29).

- 업로드 job의 늦은 벡터 이벤트: `pending-vector:<jobId>`(TTL 7일). 업로드 집계(R-26)와
  재발행(R-27)이 읽는다.
- 실시간 창: `pending:<sourceId>:<chunkId>`(TTL 1시간). 창 완료(R-30)가 읽는다.

해시 필드가 eventId·executionId라 같은 메시지를 다시 받아도 결과가 같다(최소 한 번 전달).
"""

from dataclasses import dataclass
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel
from vlm_contracts.events import ChunkManifest, EventAnalyzed, EventType, VLMAnalysis

from aggregator.kv import KeyValueStore
from aggregator.model import InputEvent

VECTOR_TTL_S = 7 * 24 * 3600
"""늦은 벡터 이벤트 대기 집합 TTL(R-25: 7일)."""
WINDOW_TTL_S = 3600
"""실시간 창 대기 집합 TTL(R-29: 1시간)."""


class _Stored(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class StoredEvent(_Stored):
    """대기 집합의 이벤트 요약. 계산에 쓰는 필드와, 실시간 재판정(R-31)이 대표 크롭을 찾을 트랙
    로컬 ID·실행 ID만 둔다."""

    event_id: UUID
    chunk_id: UUID | None
    event_type: EventType
    start_ms: int
    end_ms: int
    track_id: UUID | None
    zone_id: UUID | None
    zone_name: str | None
    judgment_method: str
    score: float | None
    analysis: VLMAnalysis
    execution_id: UUID | None = None
    track_local_id: str | None = None

    @classmethod
    def of(cls, event: InputEvent) -> "StoredEvent":
        return cls(
            event_id=event.event_id,
            chunk_id=event.chunk_id,
            event_type=event.event_type,
            start_ms=event.start_ms,
            end_ms=event.end_ms,
            track_id=event.track_id,
            zone_id=event.zone_id,
            zone_name=event.zone_name,
            judgment_method=event.judgment_method,
            score=event.score,
            analysis=event.analysis,
        )

    @classmethod
    def from_analyzed(cls, message: EventAnalyzed) -> "StoredEvent":
        """`event.analyzed`를 계산 입력으로 바꾼다.

        업로드 이벤트의 최종 trackId는 track-stitcher 번역표가 정해 여기서는 알 수 없어 비운다.
        실시간은 stitcher를 건너뛰므로(ADR-0015) 실행 ID와 트랙 로컬 ID로 트랙 ID를 만든다
        (`stream_track_id`).
        """
        detector = message.detector_candidate
        vector = message.vector_candidate
        local = detector.track_local_id if detector else None
        track = (
            stream_track_id(message.execution_id, local)
            if message.job_type == "STREAM" and local is not None
            else None
        )
        return cls(
            event_id=message.event_id,
            chunk_id=message.chunk_id,
            event_type=message.analysis.event_type,
            start_ms=message.start_ms,
            end_ms=message.end_ms,
            track_id=track,
            zone_id=detector.zone_id if detector else None,
            zone_name=detector.zone_name if detector else None,
            judgment_method=message.judgment_method,
            score=detector.score if detector else (vector.score if vector else None),
            analysis=message.analysis,
            execution_id=message.execution_id,
            track_local_id=local,
        )

    def to_input(self) -> InputEvent:
        return InputEvent(
            event_id=self.event_id,
            chunk_id=self.chunk_id,
            event_type=self.event_type,
            start_ms=self.start_ms,
            end_ms=self.end_ms,
            track_id=self.track_id,
            zone_id=self.zone_id,
            zone_name=self.zone_name,
            judgment_method=self.judgment_method,
            score=self.score,
            analysis=self.analysis,
        )


def stream_track_id(execution_id: UUID, track_local_id: str) -> UUID:
    """실시간 트랙 ID. 같은 실행의 같은 로컬 트랙이면 같은 ID다."""
    return uuid5(execution_id, track_local_id)


class StoredTracks(_Stored):
    """실시간 창의 트랙 요약 파일 위치(`track.detected` origin chunk)."""

    execution_id: UUID
    tracks_key: str


class WindowManifest(_Stored):
    """실시간 창의 소유 범위·빈 시간·마지막 창 여부(`chunk.manifest`)."""

    start_ms: int
    end_ms: int
    gap_ms: int
    last: bool

    @classmethod
    def of(cls, manifest: ChunkManifest) -> "WindowManifest":
        return cls(
            start_ms=manifest.owned_range.start_ms,
            end_ms=manifest.owned_range.end_ms,
            gap_ms=manifest.gap_ms or 0,
            last=bool(manifest.last),
        )


@dataclass(frozen=True, slots=True)
class WindowInputs:
    """창 하나의 대기 집합."""

    events: tuple[StoredEvent, ...]
    tracks: tuple[StoredTracks, ...]
    manifest: WindowManifest | None


class PendingVectors:
    """업로드 job의 늦은 벡터 이벤트(R-25)."""

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    async def add(self, job_id: UUID, event: StoredEvent) -> None:
        await self._store.hset(
            _vector_key(job_id), str(event.event_id), event.model_dump_json(), VECTOR_TTL_S
        )

    async def all(self, job_id: UUID) -> list[StoredEvent]:
        found = await self._store.hgetall(_vector_key(job_id))
        return sorted(
            (StoredEvent.model_validate_json(v) for v in found.values()),
            key=lambda e: e.event_id,
        )


class PendingWindows:
    """실시간 창 입력(R-29)."""

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    async def add_event(self, source_id: UUID, chunk_id: UUID, event: StoredEvent) -> None:
        await self._put(source_id, chunk_id, f"event:{event.event_id}", event.model_dump_json())

    async def add_tracks(self, source_id: UUID, chunk_id: UUID, tracks: StoredTracks) -> None:
        await self._put(
            source_id, chunk_id, f"tracks:{tracks.execution_id}", tracks.model_dump_json()
        )

    async def set_manifest(self, source_id: UUID, chunk_id: UUID, manifest: WindowManifest) -> None:
        await self._put(source_id, chunk_id, "manifest", manifest.model_dump_json())

    async def load(self, source_id: UUID, chunk_id: UUID) -> WindowInputs:
        found = await self._store.hgetall(_window_key(source_id, chunk_id))
        events = sorted(
            (
                StoredEvent.model_validate_json(v)
                for k, v in found.items()
                if k.startswith("event:")
            ),
            key=lambda e: e.event_id,
        )
        tracks = sorted(
            (
                StoredTracks.model_validate_json(v)
                for k, v in found.items()
                if k.startswith("tracks:")
            ),
            key=lambda t: t.execution_id,
        )
        manifest = found.get("manifest")
        return WindowInputs(
            tuple(events),
            tuple(tracks),
            WindowManifest.model_validate_json(manifest) if manifest else None,
        )

    async def _put(self, source_id: UUID, chunk_id: UUID, field: str, value: str) -> None:
        await self._store.hset(_window_key(source_id, chunk_id), field, value, WINDOW_TTL_S)


def _vector_key(job_id: UUID) -> str:
    return f"pending-vector:{job_id}"


def _window_key(source_id: UUID, chunk_id: UUID) -> str:
    return f"pending:{source_id}:{chunk_id}"
