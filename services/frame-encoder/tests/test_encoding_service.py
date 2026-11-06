from fe_support import (
    DIMENSION,
    MODEL_ID,
    NOW,
    PREFIX,
    RecordingPublisher,
    encoder,
    image_bytes,
    segment_ready,
    storage_with_images,
)
from frame_encoder.service import EncoderConfig, FrameEncoderService
from frame_encoder.vectors import segment_vector
from vlm_contracts.events import SegmentEmbedded, SegmentReady
from vlm_contracts.vectors import VectorFile
from vlm_messaging.worker import MessageContext
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
