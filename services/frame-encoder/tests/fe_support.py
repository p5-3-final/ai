"""frame-encoder 테스트가 함께 쓰는 메시지·저장소 준비 도우미."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

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
