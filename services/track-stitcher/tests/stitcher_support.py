"""track-stitcher 테스트가 함께 쓰는 메시지·파일·발행기·계약 검증 도우미."""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from track_stitcher.profile import (
    BoundaryProfile,
    FinalizeProfile,
    ReidProfile,
    StitchProfile,
)
from vlm_contracts.events import (
    ChunkBoundary,
    Event,
    JobType,
    SegmentEmbedded,
    TimeRange,
    VideoDeleted,
)
from vlm_contracts.tracks import BoundaryBox, ChunkTrack, ChunkTracks
from vlm_contracts.vectors import CropVector, VectorFile
from vlm_storage.storage import ObjectStorage

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f100-0000-7000-8000-000000000001")
NORMALIZATION = UUID("0192f100-0000-7000-8000-000000000004")
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
EMBEDDING_MODEL = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
OTHER_MODEL = "open_clip:ViT-B-32:laion2b:mean-4"
DETECTOR = "industrial-default@1"
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
CHUNK_MS = 120_000
OVERLAP_MS = 5_000
DIMENSION = 4

PROFILE = StitchProfile(
    version="test-1",
    boundary=BoundaryProfile(min_mean_iou=0.5, min_common_observations=2, time_tolerance_ms=100),
    reid=ReidProfile(min_similarity=0.8, max_gap_ms=60_000),
    finalize=FinalizeProfile(wait_timeout_s=1800),
)

type Box = tuple[float, float, float, float]
LEFT: Box = (0.1, 0.2, 0.3, 0.8)
RIGHT: Box = (0.6, 0.2, 0.8, 0.8)


def chunk_id(index: int, source: UUID = SOURCE) -> UUID:
    return UUID(int=(source.int ^ 0x1000) + index)


def execution_id(index: int, attempt: int = 0, source: UUID = SOURCE) -> UUID:
    return UUID(int=(source.int ^ 0x2000) + index * 16 + attempt)


def owned(index: int, duration_ms: int) -> TimeRange:
    return TimeRange(start_ms=index * CHUNK_MS, end_ms=min((index + 1) * CHUNK_MS, duration_ms))


def processing(index: int, duration_ms: int) -> TimeRange:
    own = owned(index, duration_ms)
    return TimeRange(
        start_ms=max(own.start_ms - OVERLAP_MS, 0), end_ms=min(own.end_ms + OVERLAP_MS, duration_ms)
    )


def boxes(start_ms: int, end_ms: int, box: Box = LEFT, step_ms: int = 500) -> list[BoundaryBox]:
    """[start_ms, end_ms) 안에 `step_ms` 간격으로 같은 박스를 둔다."""
    return [BoundaryBox(pts_ms=t, box=list(box)) for t in range(start_ms, end_ms, step_ms)]


def track(
    local_id: str,
    start_ms: int,
    end_ms: int,
    *,
    class_name: str = "person",
    head: Sequence[BoundaryBox] = (),
    tail: Sequence[BoundaryBox] = (),
) -> ChunkTrack:
    return ChunkTrack(
        track_local_id=local_id,
        class_name=class_name,
        start_ms=start_ms,
        end_ms=end_ms,
        touches_head=bool(head),
        touches_tail=bool(tail),
        observation_count=max(1, len(head) + len(tail)),
        boundary_boxes=[*head, *tail],
    )


def chunk_tracks(
    index: int,
    tracks: Sequence[ChunkTrack] = (),
    *,
    duration_ms: int = 5 * CHUNK_MS,
    attempt: int = 0,
    source: UUID = SOURCE,
    job: UUID = JOB,
) -> ChunkTracks:
    return ChunkTracks(
        source_id=source,
        job_id=job,
        chunk_id=chunk_id(index, source),
        execution_id=execution_id(index, attempt, source),
        normalization_id=NORMALIZATION,
        owned_range=owned(index, duration_ms),
        processing_range=processing(index, duration_ms),
        detector_profile=DETECTOR,
        tracks=list(tracks),
    )


def tracks_key(tracks: ChunkTracks) -> str:
    return (
        f"sources/{tracks.source_id}/jobs/{tracks.job_id}/chunks/{tracks.chunk_id}"
        f"/executions/{tracks.execution_id}/tracks.json"
    )


def boundary_message(
    tracks: ChunkTracks,
    *,
    duration_ms: int = 5 * CHUNK_MS,
    job_type: JobType | None = None,
    event_id: UUID | None = None,
) -> ChunkBoundary:
    return ChunkBoundary(
        event_id=event_id or UUID(int=tracks.execution_id.int ^ 0xB),
        occurred_at=NOW,
        source_id=tracks.source_id,
        owner_id=OWNER,
        job_type=job_type,
        job_id=tracks.job_id,
        chunk_id=tracks.chunk_id,
        execution_id=tracks.execution_id,
        normalization_id=tracks.normalization_id,
        owned_range=tracks.owned_range,
        processing_range=tracks.processing_range,
        source_duration_ms=duration_ms,
        tracks_key=tracks_key(tracks),
        track_count=len(tracks.tracks),
        boundary_track_count=sum(t.touches_head or t.touches_tail for t in tracks.tracks),
        detector_profile=tracks.detector_profile,
    )


def unit(*values: float) -> list[float]:
    norm = sum(v * v for v in values) ** 0.5
    return [v / norm for v in values]


def vector_file(
    tracks: ChunkTracks,
    crops: dict[str, Sequence[list[float]]] | None = None,
    *,
    model: str = EMBEDDING_MODEL,
) -> VectorFile:
    """`crops`는 trackLocalId → 크롭 벡터 목록이다. 없으면 트랙마다 같은 방향 벡터 하나다."""
    chosen = (
        crops
        if crops is not None
        else {t.track_local_id: [unit(1, 0, 0, 0)] for t in tracks.tracks}
    )
    dimension = len(next((v for vs in chosen.values() for v in vs), [0.0] * DIMENSION))
    return VectorFile(
        source_id=tracks.source_id,
        job_id=tracks.job_id,
        chunk_id=tracks.chunk_id,
        execution_id=tracks.execution_id,
        normalization_id=tracks.normalization_id,
        embedding_model=model,
        dimension=dimension,
        segments=[],
        crops=[
            CropVector(
                crop_id=UUID(int=tracks.execution_id.int ^ (i + 1) << 8 ^ j),
                track_local_id=local_id,
                pts_ms=tracks.owned_range.start_ms,
                vector=vector,
            )
            for i, (local_id, vectors) in enumerate(sorted(chosen.items()))
            for j, vector in enumerate(vectors)
        ],
    )


def vectors_key(vectors: VectorFile) -> str:
    return (
        f"sources/{vectors.source_id}/jobs/{vectors.job_id}/chunks/{vectors.chunk_id}"
        f"/executions/{vectors.execution_id}/vectors.json"
    )


def embedded_message(vectors: VectorFile, *, job_type: JobType | None = None) -> SegmentEmbedded:
    return SegmentEmbedded(
        event_id=UUID(int=vectors.execution_id.int ^ 0xE),
        occurred_at=NOW,
        source_id=vectors.source_id,
        owner_id=OWNER,
        job_type=job_type,
        job_id=vectors.job_id,
        chunk_id=vectors.chunk_id,
        execution_id=vectors.execution_id,
        normalization_id=vectors.normalization_id,
        vectors_key=vectors_key(vectors),
        segment_count=len(vectors.segments),
        crop_count=len(vectors.crops),
        embedding_model=vectors.embedding_model,
        dimension=vectors.dimension,
    )


def deleted_message(source: UUID = SOURCE) -> VideoDeleted:
    return VideoDeleted(
        event_id=UUID(int=source.int ^ 0xD), occurred_at=NOW, source_id=source, owner_id=OWNER
    )


def put(storage: ObjectStorage, key: str, model: BaseModel) -> None:
    storage.put_bytes(key, model.model_dump_json(by_alias=True).encode(), "application/json")


def store_chunk(storage: ObjectStorage, tracks: ChunkTracks) -> None:
    put(storage, tracks_key(tracks), tracks)


def store_vectors(storage: ObjectStorage, vectors: VectorFile) -> None:
    put(storage, vectors_key(vectors), vectors)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)
    fail: bool = False

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        if self.fail:
            from vlm_messaging.publisher import PublishError

            raise PublishError("broker unavailable")
        self.published.append((topic, event, key, traceparent))

    def events(self, topic: str) -> list[Event]:
        return [e for t, e, _, _ in self.published if t == topic]


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


@cache
def _registry() -> tuple[Registry[Schema], Path]:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "events").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    # 스키마는 서로를 상대 경로로 참조하므로 각자의 $id로 등록한다.
    resources = []
    for schema in [
        *sorted((path / "events").glob("*.schema.json")),
        *sorted((path / "tracks/v1").glob("*.schema.json")),
        path / "taxonomy/v2/taxonomy.schema.json",
    ]:
        raw = json.loads(schema.read_text(encoding="utf-8"))
        resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    return Registry().with_resources(resources), path


def _errors(schema_path: str, body: object) -> list[str]:
    registry, path = _registry()
    schema = json.loads((path / schema_path).read_text(encoding="utf-8"))
    validator = Draft202012Validator({"$ref": schema["$id"]}, registry=registry)
    return [e.message for e in validator.iter_errors(body)]


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    body = json.loads(event.model_dump_json(by_alias=True))
    return _errors(f"events/{event.event_type}.schema.json", body)


def mapping_errors(raw: bytes) -> list[str]:
    """저장한 번역표 파일을 docs 원본 스키마로 검증한다."""
    return _errors("tracks/v1/track-mapping.schema.json", json.loads(raw))
