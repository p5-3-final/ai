"""frame-encoder 테스트가 함께 쓰는 메시지·저장소 준비 도우미."""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator
from pydantic import Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from vlm_contracts.events import Event, ReadyCrop, ReadySegment, SegmentFrame, SegmentReady
from vlm_encoders.mock import MockEncoder
from vlm_storage.memory import InMemoryStorage

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f0d4-0000-7000-8000-000000000001")
CHUNK = UUID("0192f0d4-0000-7000-8000-000000000002")
EXECUTION = UUID("0192f0d4-0000-7000-8000-000000000003")
NORMALIZATION = UUID("0192f0d4-0000-7000-8000-000000000004")
READY_ID = UUID("0192f0d4-0000-7000-8000-000000000005")
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
MODEL_ID = "mock:mock:mock:mean4"
DIMENSION = 8
PREFIX = f"sources/{SOURCE}/jobs/{JOB}/chunks/{CHUNK}/executions/{EXECUTION}/"


def encoder() -> MockEncoder:
    return MockEncoder(MODEL_ID, DIMENSION)


def frame_key(segment: int, frame: int) -> str:
    return f"{PREFIX}blurred/seg{segment:02d}_f{frame}.jpg"


def crop_key(crop: int) -> str:
    return f"{PREFIX}blurred/crop{crop:03d}.jpg"


def image_bytes(key: str) -> bytes:
    """키마다 다른 이미지 바이트. mock 인코더는 바이트 해시로 벡터를 만든다."""
    return f"image:{key}".encode()


def segment_ready(segments: int = 2, crops: int = 3) -> SegmentReady:
    return SegmentReady(
        event_id=READY_ID,
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        job_id=JOB,
        chunk_id=CHUNK,
        execution_id=EXECUTION,
        normalization_id=NORMALIZATION,
        segments=[
            ReadySegment(
                segment_id=UUID(int=0x1000 + s),
                start_ms=s * 10_000,
                end_ms=(s + 1) * 10_000,
                # 두 번째 구간은 정적 구간이다(R-7).
                static=s % 2 == 1,
                frames=[
                    SegmentFrame(pts_ms=s * 10_000 + f * 2_500, key=frame_key(s, f))
                    for f in range(4)
                ],
            )
            for s in range(segments)
        ],
        crops=[
            ReadyCrop(
                crop_id=UUID(int=0x2000 + c),
                track_local_id=f"t{c}",
                pts_ms=c * 1_000,
                key=crop_key(c),
            )
            for c in range(crops)
        ],
    )


def put_images(storage: InMemoryStorage, ready: SegmentReady) -> InMemoryStorage:
    keys = [f.key for s in ready.segments for f in s.frames] + [c.key for c in ready.crops]
    for key in keys:
        storage.put_bytes(key, image_bytes(key), "image/jpeg")
    return storage


def storage_with_images(ready: SegmentReady) -> InMemoryStorage:
    return put_images(InMemoryStorage(), ready)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
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
def _event_registry() -> tuple[Registry[Schema], str]:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "events").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    # 스키마는 서로를 상대 경로로 참조하므로 각자의 $id로 등록한다.
    resources = []
    for schema in [
        *sorted((path / "events").glob("*.schema.json")),
        path / "taxonomy/v2/taxonomy.schema.json",
    ]:
        raw = json.loads(schema.read_text(encoding="utf-8"))
        resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    envelope = json.loads((path / "events/_envelope.schema.json").read_text(encoding="utf-8"))
    return Registry().with_resources(resources), str(envelope["$id"])


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    registry, envelope_id = _event_registry()
    schema_id = envelope_id.replace("_envelope", event.event_type)
    validator = Draft202012Validator({"$ref": schema_id}, registry=registry)
    body = json.loads(event.model_dump_json(by_alias=True))
    return [e.message for e in validator.iter_errors(body)]
