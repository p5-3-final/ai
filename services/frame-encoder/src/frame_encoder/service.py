"""`segment.ready` 하나를 벡터 파일과 `segment.embedded`로 바꾸는 처리 순서(SPEC-019)."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from vlm_contracts.events import Event, SegmentEmbedded, SegmentReady
from vlm_contracts.vectors import CropVector, SegmentVector, VectorFile
from vlm_encoders.base import ImageDecodeError, ImageTextEncoder, Vector
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from frame_encoder import ids
from frame_encoder.vectors import segment_vector

# 계약상 구간 하나는 프레임 4장이다(segment.ready.schema.json).
_FRAMES_PER_SEGMENT = 4


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EncoderConfig:
    """인코딩 설정.

    Attributes:
        batch_size: 인코더에 한 번에 넣는 이미지 수. 메시지 하나가 이미지 768장까지 담을 수 있어
            나눠 넣어 메모리를 묶는다.
    """

    batch_size: int


class FrameEncoderService:
    """구간·크롭 이미지를 인코딩해 벡터 파일로 저장하고 `segment.embedded`를 발행한다.

    저장소·Kafka 장애는 예외를 그대로 던져 워커가 다시 시도하게 한다(SPEC-019 R-18).
    """

    def __init__(
        self,
        storage: ObjectStorage,
        encoder: ImageTextEncoder,
        publisher: EventPublisher,
        config: EncoderConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._encoder = encoder
        self._publisher = publisher
        self._config = config
        self._clock = clock

    async def handle(self, event: SegmentReady, ctx: MessageContext) -> None:
        """워커 경로 핸들러. 인코딩과 저장소 호출이 블로킹이라 스레드에서 처리한다.

        이벤트 루프를 막지 않아야 처리 중에도 소비 그룹 하트비트가 이어진다(SPEC-019 NFR-3).
        """
        await asyncio.to_thread(self._process, event, ctx.traceparent)

    def _process(self, ready: SegmentReady, traceparent: str | None) -> None:
        frames = self._encode([f.key for s in ready.segments for f in s.frames])
        segments = [
            SegmentVector(
                segment_id=s.segment_id,
                start_ms=s.start_ms,
                end_ms=s.end_ms,
                static=s.static,
                vector=segment_vector(
                    frames[i * _FRAMES_PER_SEGMENT : (i + 1) * _FRAMES_PER_SEGMENT]
                ),
            )
            for i, s in enumerate(ready.segments)
        ]
        crop_vectors = self._encode([c.key for c in ready.crops])
        crops = [
            CropVector(
                crop_id=c.crop_id, track_local_id=c.track_local_id, pts_ms=c.pts_ms, vector=v
            )
            for c, v in zip(ready.crops, crop_vectors, strict=True)
        ]
        vector_file = VectorFile(
            source_id=ready.source_id,
            job_id=ready.job_id,
            chunk_id=ready.chunk_id,
            execution_id=ready.execution_id,
            normalization_id=ready.normalization_id,
            embedding_model=self._encoder.model_id,
            dimension=self._encoder.dimension,
            segments=segments,
            crops=crops,
        )
        key = ids.vectors_key(ready)
        # 같은 실행은 같은 키·같은 내용이라 다시 처리할 때 덮어써도 된다.
        self._storage.put_bytes(
            key, vector_file.model_dump_json(by_alias=True).encode(), "application/json"
        )
        embedded = SegmentEmbedded(
            event_id=ids.embedded_event_id(ready.execution_id),
            occurred_at=self._clock(),
            source_id=ready.source_id,
            owner_id=ready.owner_id,
            job_id=ready.job_id,
            chunk_id=ready.chunk_id,
            execution_id=ready.execution_id,
            normalization_id=ready.normalization_id,
            vectors_key=key,
            segment_count=len(segments),
            crop_count=len(crops),
            embedding_model=self._encoder.model_id,
            dimension=self._encoder.dimension,
        )
        self._publish(embedded, traceparent)

    def _encode(self, keys: list[str]) -> list[Vector]:
        """이미지를 묶음으로 나눠 읽고 인코딩한다.

        Raises:
            NonRetryableError: 이미지가 없거나 디코딩할 수 없을 때(SPEC-019 R-17). 다시 시도해도
                결과가 같아 DLQ로 보낸다. 저장소 일시 오류는 그대로 던져 재시도한다(R-18).
        """
        vectors: list[Vector] = []
        size = self._config.batch_size
        for start in range(0, len(keys), size):
            try:
                images = [self._storage.get_bytes(k) for k in keys[start : start + size]]
                vectors.extend(self._encoder.encode_images(images))
            except (ObjectNotFoundError, ImageDecodeError) as e:
                raise NonRetryableError(f"input image is unusable: {e.code}") from e
        return vectors

    def _publish(self, event: Event, traceparent: str | None) -> None:
        self._publisher.publish(
            event.event_type, prepare_event(event, self._clock()), traceparent=traceparent
        )
