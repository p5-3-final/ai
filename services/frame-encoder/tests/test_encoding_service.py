from dataclasses import replace
from uuid import UUID

import pytest
from fe_support import (
    DIMENSION,
    MODEL_ID,
    NOW,
    PREFIX,
    RecordingPublisher,
    crop_key,
    encoder,
    event_errors,
    frame_key,
    image_bytes,
    put_images,
    segment_ready,
    storage_with_images,
)
from frame_encoder.profile import PrototypesProfile, PrototypeType
from frame_encoder.prototypes import Prototypes
from frame_encoder.service import EncoderConfig, FrameEncoderService
from frame_encoder.vectors import cosine, segment_vector
from vlm_contracts.events import EventCandidateVector, SegmentEmbedded, SegmentReady
from vlm_contracts.vectors import VectorFile
from vlm_encoders.base import ImageDecodeError, Vector
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.memory import InMemoryStorage

CTX = MessageContext(
    topic="segment.ready", partition=0, offset=0, key=None, attempt=1, traceparent="00-t-s-01"
)
VECTORS_KEY = f"{PREFIX}vectors.json"


def _service(storage: InMemoryStorage, publisher: RecordingPublisher) -> FrameEncoderService:
    return FrameEncoderService(
        storage, encoder(), publisher, EncoderConfig(batch_size=3), clock=lambda: NOW
    )


async def _run(ready: SegmentReady) -> tuple[InMemoryStorage, RecordingPublisher]:
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()
    await _service(storage, publisher).handle(ready, CTX)
    return storage, publisher


def _vector_file(storage: InMemoryStorage) -> VectorFile:
    assert storage.exists(VECTORS_KEY)
    return VectorFile.model_validate_json(storage.get_bytes(VECTORS_KEY))


async def test_spec_019_r1_r7_stores_segment_vectors_with_static_flag() -> None:
    ready = segment_ready(segments=2, crops=0)

    storage, _ = await _run(ready)

    stored = _vector_file(storage)
    mock = encoder()
    for segment, saved in zip(ready.segments, stored.segments, strict=True):
        frames = mock.encode_images([image_bytes(f.key) for f in segment.frames])
        assert saved.segment_id == segment.segment_id
        assert (saved.start_ms, saved.end_ms) == (segment.start_ms, segment.end_ms)
        assert saved.static is segment.static
        assert saved.vector == segment_vector(frames)
    assert [s.static for s in stored.segments] == [False, True]


async def test_spec_019_r2_stores_crop_vectors_from_same_encoder() -> None:
    ready = segment_ready(segments=0, crops=5)

    storage, _ = await _run(ready)

    stored = _vector_file(storage)
    expected = encoder().encode_images([image_bytes(c.key) for c in ready.crops])
    assert [c.crop_id for c in stored.crops] == [c.crop_id for c in ready.crops]
    assert [c.track_local_id for c in stored.crops] == [c.track_local_id for c in ready.crops]
    assert [c.pts_ms for c in stored.crops] == [c.pts_ms for c in ready.crops]
    assert [c.vector for c in stored.crops] == expected


async def test_spec_019_r3_r8_vector_file_identifies_execution_and_model() -> None:
    ready = segment_ready()

    storage, _ = await _run(ready)

    stored = _vector_file(storage)
    assert (stored.source_id, stored.job_id, stored.chunk_id, stored.execution_id) == (
        ready.source_id,
        ready.job_id,
        ready.chunk_id,
        ready.execution_id,
    )
    assert stored.normalization_id == ready.normalization_id
    assert (stored.embedding_model, stored.dimension) == (MODEL_ID, DIMENSION)


async def test_spec_019_r4_publishes_segment_embedded_after_storing() -> None:
    ready = segment_ready(segments=2, crops=3)

    storage, publisher = await _run(ready)

    [(topic, event, key, traceparent)] = publisher.published
    assert topic == "segment.embedded"
    assert isinstance(event, SegmentEmbedded)
    assert event.vectors_key == VECTORS_KEY
    assert (event.segment_count, event.crop_count) == (2, 3)
    assert (event.embedding_model, event.dimension) == (MODEL_ID, DIMENSION)
    assert (event.job_id, event.chunk_id, event.execution_id) == (
        ready.job_id,
        ready.chunk_id,
        ready.execution_id,
    )
    assert event.owner_id == ready.owner_id
    assert event.produced_at == NOW
    assert key is None
    assert traceparent == CTX.traceparent
    assert storage.exists(event.vectors_key)


async def test_spec_019_r5_reprocessing_uses_same_event_id_and_key() -> None:
    ready = segment_ready()
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()
    service = _service(storage, publisher)

    await service.handle(ready, CTX)
    first = storage.get_bytes(VECTORS_KEY)
    await service.handle(ready, CTX)

    [a, b] = publisher.events("segment.embedded")
    assert isinstance(a, SegmentEmbedded)
    assert isinstance(b, SegmentEmbedded)
    assert a.event_id == b.event_id
    assert a.vectors_key == b.vectors_key == VECTORS_KEY
    assert storage.get_bytes(VECTORS_KEY) == first


async def test_spec_019_r5_other_execution_gets_other_event_id() -> None:
    # 이미지 경로는 실행 폴더에 묶이므로, 실행 ID만 다른 메시지는 구간·크롭 없이 만든다.
    ready = segment_ready(segments=0, crops=0)
    other = ready.model_copy(update={"execution_id": UUID(int=0x9999)})

    _, publisher = await _run(ready)
    other_publisher = RecordingPublisher()
    await _service(InMemoryStorage(), other_publisher).handle(other, CTX)

    [a] = publisher.events("segment.embedded")
    [b] = other_publisher.events("segment.embedded")
    assert a.event_id != b.event_id


async def test_spec_019_r6_empty_message_stores_empty_file_and_publishes() -> None:
    ready = segment_ready(segments=0, crops=0)

    storage, publisher = await _run(ready)

    stored = _vector_file(storage)
    assert (stored.segments, stored.crops) == ([], [])
    [event] = publisher.events("segment.embedded")
    assert isinstance(event, SegmentEmbedded)
    assert (event.segment_count, event.crop_count) == (0, 0)


class UnreadableImageEncoder:
    """특정 이미지 바이트를 디코딩하지 못하는 인코더."""

    def __init__(self, unreadable: bytes) -> None:
        self._inner = encoder()
        self._unreadable = unreadable

    @property
    def model_id(self) -> str:
        return self._inner.model_id

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    def encode_texts(self, texts: list[str]) -> list[Vector]:
        return self._inner.encode_texts(texts)

    def encode_images(self, images: list[bytes]) -> list[Vector]:
        if self._unreadable in images:
            raise ImageDecodeError("cannot decode image")
        return self._inner.encode_images(images)


async def test_spec_019_r17_missing_image_goes_to_dlq_without_output() -> None:
    ready = segment_ready()
    storage = storage_with_images(ready)
    storage.delete_prefix(crop_key(1))
    publisher = RecordingPublisher()

    with pytest.raises(NonRetryableError):
        await _service(storage, publisher).handle(ready, CTX)

    assert not storage.exists(VECTORS_KEY)
    assert publisher.published == []


async def test_spec_019_r17_undecodable_image_goes_to_dlq_without_output() -> None:
    ready = segment_ready()
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()
    bad = UnreadableImageEncoder(image_bytes(frame_key(1, 2)))
    service = FrameEncoderService(storage, bad, publisher, EncoderConfig(batch_size=3))

    with pytest.raises(NonRetryableError):
        await service.handle(ready, CTX)

    assert not storage.exists(VECTORS_KEY)
    assert publisher.published == []


class UnavailableStorage(InMemoryStorage):
    """읽기나 쓰기가 일시적으로 실패하는 저장소."""

    def __init__(self, *, fail_read: bool = False, fail_write: bool = False) -> None:
        super().__init__()
        self._fail_read = fail_read
        self._fail_write = fail_write

    def get_bytes(self, key: str) -> bytes:
        if self._fail_read:
            raise ConnectionError("storage unavailable")
        return super().get_bytes(key)

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        if self._fail_write and key == VECTORS_KEY:
            raise ConnectionError("storage unavailable")
        super().put_bytes(key, data, content_type)


@pytest.mark.parametrize("failure", ["read", "write"])
async def test_spec_019_r18_transient_storage_error_is_retried(failure: str) -> None:
    ready = segment_ready()
    storage = UnavailableStorage(fail_read=failure == "read", fail_write=failure == "write")
    put_images(storage, ready)
    publisher = RecordingPublisher()

    # 재시도 토픽으로 보내려면 NonRetryableError가 아닌 원래 오류가 나가야 한다.
    with pytest.raises(ConnectionError):
        await _service(storage, publisher).handle(ready, CTX)

    assert publisher.published == []


def _timed_ctx() -> MessageContext:
    return replace(CTX, timing=StageRecorder(service="frame-encoder", attempt=1, received_at=NOW))


async def test_spec_019_r19_records_segment_crop_and_store_stages() -> None:
    ready = segment_ready(segments=2, crops=3)
    storage = storage_with_images(ready)
    ctx = _timed_ctx()

    await _service(storage, RecordingPublisher()).handle(ready, ctx)

    timings = ctx.timing.timings
    assert [(t.stage_id, t.status) for t in timings] == [
        ("encoder.segments", "ok"),
        ("encoder.crops", "ok"),
        ("encoder.store", "ok"),
    ]
    # 단계마다 처리한 개수: 구간 2개(이미지 8장), 크롭 3장, 파일 1개
    assert [(t.count, t.unit) for t in timings] == [
        (2, "segments"),
        (3, "crops"),
        (1, "files"),
    ]


async def test_spec_019_r19_failed_stage_is_recorded_as_error() -> None:
    ready = segment_ready(segments=1, crops=2)
    storage = storage_with_images(ready)
    storage.delete_prefix(crop_key(0))
    ctx = _timed_ctx()

    with pytest.raises(NonRetryableError):
        await _service(storage, RecordingPublisher()).handle(ready, ctx)

    assert [(t.stage_id, t.status) for t in ctx.timing.timings] == [
        ("encoder.segments", "ok"),
        ("encoder.crops", "error"),
    ]


FALL_TEXTS = ["a worker lying on the floor"]


def _prototypes(threshold: float) -> Prototypes:
    profile = PrototypesProfile(
        version="local-sample-v1",
        types={"fall": PrototypeType(texts=FALL_TEXTS, threshold=threshold)},
    )
    return Prototypes.build(profile, encoder())


def _with_prototypes(
    storage: InMemoryStorage, publisher: RecordingPublisher, prototypes: Prototypes | None
) -> FrameEncoderService:
    return FrameEncoderService(
        storage,
        encoder(),
        publisher,
        EncoderConfig(batch_size=3, prototypes=prototypes),
        clock=lambda: NOW,
    )


async def test_spec_019_r12_r14_candidates_follow_segment_embedded() -> None:
    # 임계값 -0.99면 모든 구간이 후보다(mock 벡터는 의미가 없어 점수를 미리 알 수 없다).
    ready = segment_ready(segments=3, crops=1)
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()

    await _with_prototypes(storage, publisher, _prototypes(-0.99)).handle(ready, CTX)

    assert [t for t, _, _, _ in publisher.published] == [
        "segment.embedded",
        "event.candidate.vector",
    ]
    [event] = publisher.events("event.candidate.vector")
    assert isinstance(event, EventCandidateVector)
    assert event_errors(event) == []
    assert event.embedding_model == MODEL_ID
    assert (event.prototype_version, event.taxonomy_version) == ("local-sample-v1", "v2")
    assert (event.job_id, event.chunk_id, event.execution_id) == (
        ready.job_id,
        ready.chunk_id,
        ready.execution_id,
    )
    assert event.produced_at == NOW
    stored = _vector_file(storage)
    fall = _prototypes(-0.99).vector("fall")
    for candidate, segment, vector in zip(
        event.candidates, ready.segments, stored.segments, strict=True
    ):
        assert candidate.segment_id == segment.segment_id
        assert (candidate.start_ms, candidate.end_ms) == (segment.start_ms, segment.end_ms)
        assert candidate.event_type == "fall"
        assert candidate.score == pytest.approx(cosine(vector.vector, fall))
        assert candidate.threshold == -0.99
        assert candidate.frame_keys == [f.key for f in segment.frames]


async def test_spec_019_r5_r14_candidate_ids_are_deterministic() -> None:
    ready = segment_ready(segments=2, crops=0)
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()
    service = _with_prototypes(storage, publisher, _prototypes(-0.99))

    await service.handle(ready, CTX)
    await service.handle(ready, CTX)

    first, second = publisher.events("event.candidate.vector")
    assert isinstance(first, EventCandidateVector)
    assert isinstance(second, EventCandidateVector)
    assert first.event_id == second.event_id
    assert [c.candidate_id for c in first.candidates] == [c.candidate_id for c in second.candidates]
    assert len({c.candidate_id for c in first.candidates}) == 2
    embedded = publisher.events("segment.embedded")[0]
    assert first.event_id != embedded.event_id


async def test_spec_019_r13_no_prototypes_publishes_only_segment_embedded() -> None:
    ready = segment_ready()

    _, publisher = await _run(ready)

    assert [t for t, _, _, _ in publisher.published] == ["segment.embedded"]


async def test_spec_019_r13_zero_candidates_publishes_only_segment_embedded() -> None:
    # 임계값 1이면 프로토타입과 방향이 같은 구간만 후보다. mock 이미지 벡터는 그렇지 않다.
    ready = segment_ready()
    storage = storage_with_images(ready)
    publisher = RecordingPublisher()

    await _with_prototypes(storage, publisher, _prototypes(1.0)).handle(ready, CTX)

    assert [t for t, _, _, _ in publisher.published] == ["segment.embedded"]
