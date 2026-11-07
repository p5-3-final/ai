"""핸들러 전체: 임베딩 호출, 파일 저장, 발행, 건너뜀, 오류 분류.

SPEC-025 R-4, R-5~R-10, R-12, R-13, NFR-2.
"""

import contextlib
import json
import logging
from dataclasses import replace
from uuid import UUID

import pytest
from embedding_worker.service import EmbeddingService
from embedding_worker.tracks import TrackLookup
from ew_support import (
    ANNOTATION,
    DESCRIPTION,
    DIMENSION,
    JOB,
    MODEL,
    NOW,
    SOURCE,
    TRACEPARENT,
    TRACK,
    TRACK_DESCRIPTION,
    FakeGateway,
    RecordingPublisher,
    Reply,
    event_analyzed,
    event_errors,
    gateway_error,
    mapping_key,
    segment_annotated,
    text_vector_file_errors,
    track_analyzed,
    track_mapping,
    unit_vector,
)
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import EmbedRequest, EmbedResponse
from vlm_contracts.events import (
    EmbeddingReady,
    Event,
    EventAnalyzed,
    SegmentAnnotated,
    TrackAnalyzed,
)
from vlm_messaging.publisher import PublishError
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.memory import InMemoryStorage
from vlm_storage.storage import ObjectNotFoundError

CTX = MessageContext(
    topic="event.analyzed", partition=0, offset=0, key=None, attempt=1, traceparent=TRACEPARENT
)


class FailingStorage(InMemoryStorage):
    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        raise ConnectionError("storage unavailable")


def _storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    storage.put_bytes(mapping_key(), track_mapping(TRACK), "application/json")
    return storage


async def _run(
    message: Event,
    gateway: FakeGateway | None = None,
    *,
    storage: InMemoryStorage | None = None,
    publisher: RecordingPublisher | None = None,
    ctx: MessageContext = CTX,
    max_chars: int = 2000,
) -> tuple[FakeGateway, InMemoryStorage, RecordingPublisher]:
    fake = gateway or FakeGateway()
    store = storage if storage is not None else _storage()
    sink = publisher or RecordingPublisher()
    service = EmbeddingService(
        fake, store, sink, TrackLookup(store, capacity=2), max_chars=max_chars, clock=lambda: NOW
    )
    if isinstance(message, EventAnalyzed):
        await service.handle_event(message, ctx)
    elif isinstance(message, TrackAnalyzed):
        await service.handle_track(message, ctx)
    else:
        assert isinstance(message, SegmentAnnotated)
        await service.handle_annotation(message, ctx)
    return fake, store, sink


def _published(publisher: RecordingPublisher) -> EmbeddingReady:
    [(topic, event, key, traceparent)] = publisher.published
    assert (topic, key, traceparent) == ("embedding.ready", str(SOURCE), TRACEPARENT)
    assert isinstance(event, EmbeddingReady)
    assert event_errors(event) == []
    return event


def _file(storage: InMemoryStorage, key: str) -> dict[str, object]:
    data = storage.get_bytes(key)
    assert text_vector_file_errors(data) == []
    loaded: dict[str, object] = json.loads(data)
    return loaded


# --- R-6, R-8 정상 흐름 -------------------------------------------------------------------------


async def test_spec025_r6_event_calls_embed_text_with_one_text() -> None:
    message = event_analyzed()

    gateway, _, _ = await _run(message)

    [(request, traceparent)] = gateway.requests
    assert request.task_type == "embed_text"
    assert request.texts is not None
    assert len(request.texts) == 1
    assert request.texts[0].startswith(DESCRIPTION)
    assert request.images is None
    assert traceparent == TRACEPARENT


@pytest.mark.parametrize(
    ("message", "kind", "item_id", "text_format"),
    [
        (event_analyzed(), "event", event_analyzed().event_id, "event-v1"),
        (track_analyzed(), "track", TRACK, "track-v1"),
        (segment_annotated(), "segment_annotation", ANNOTATION, "segment_annotation-v1"),
    ],
)
async def test_spec025_r6_r8_file_is_stored_then_embedding_ready_is_published(
    message: Event, kind: str, item_id: object, text_format: str
) -> None:
    _, storage, publisher = await _run(message)

    event = _published(publisher)
    key = f"sources/{SOURCE}/jobs/{JOB}/text-vectors/{kind}/{item_id}.json"
    assert (event.item_kind, str(event.item_id), event.vector_key) == (kind, str(item_id), key)
    assert (event.text_format, event.embedding_model, event.dimension) == (
        text_format,
        MODEL,
        DIMENSION,
    )
    assert (event.source_id, event.job_id, event.owner_id) == (
        message.source_id,
        JOB,
        message.owner_id,
    )
    stored = _file(storage, key)
    assert (stored["itemKind"], stored["itemId"], stored["textFormat"]) == (
        kind,
        str(item_id),
        text_format,
    )
    assert (stored["embeddingModel"], stored["dimension"]) == (MODEL, DIMENSION)
    assert stored["vector"] == unit_vector()


async def test_spec025_r7_track_file_carries_mapping_time_and_class() -> None:
    _, storage, publisher = await _run(track_analyzed())

    stored = _file(storage, _published(publisher).vector_key)
    assert stored["text"] == TRACK_DESCRIPTION
    assert stored["metadata"] == {
        "startMs": 101000,
        "endMs": 190000,
        "className": "person",
        "trackId": str(TRACK),
        "helmet": "not_worn",
        "vest": "worn",
        "role": "worker",
    }


async def test_spec025_r5_text_sent_and_stored_is_cut_to_max_chars() -> None:
    gateway, storage, publisher = await _run(event_analyzed(), max_chars=5)

    [(request, _)] = gateway.requests
    assert request.texts == [DESCRIPTION[:5]]
    assert _file(storage, _published(publisher).vector_key)["text"] == DESCRIPTION[:5]


async def test_spec025_r8_same_item_same_model_gives_same_event_id() -> None:
    _, _, first = await _run(event_analyzed())
    _, _, second = await _run(event_analyzed())

    assert _published(first).event_id == _published(second).event_id


async def test_spec025_r8_other_model_gives_other_event_id_and_overwrites_file() -> None:
    def other_model(request: EmbedRequest) -> EmbedResponse:
        return EmbedResponse(
            call_id=UUID(int=9),
            task_type=request.task_type,
            model_id="other",
            dimension=2,
            vectors=[[0.0, 1.0]],
        )

    storage = _storage()
    _, _, first = await _run(event_analyzed(), storage=storage)
    _, _, second = await _run(event_analyzed(), FakeGateway(reply=other_model), storage=storage)

    assert _published(first).event_id != _published(second).event_id
    # C-5: 경로에 모델이 없어 마지막 모델 결과로 덮어쓴다.
    assert _published(first).vector_key == _published(second).vector_key
    assert _file(storage, _published(second).vector_key)["embeddingModel"] == "other"


# --- R-13 처리 단계 ----------------------------------------------------------------------------


async def test_spec025_r13_records_call_and_store_stages() -> None:
    ctx = replace(CTX, timing=StageRecorder(service="embedding-worker", attempt=1, received_at=NOW))

    await _run(event_analyzed(), ctx=ctx)

    assert [(t.stage_id, t.status, t.count, t.unit) for t in ctx.timing.timings] == [
        ("embedding.call", "ok", 1, "texts"),
        ("embedding.store", "ok", 1, "files"),
    ]


# --- R-4 건너뜀 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (
            event_analyzed(
                description=None,
                risk_reason=None,
                solution=None,
                severity=None,
                verification_status="unknown",
            ),
            "event_no_text",
        ),
        (track_analyzed("question"), "track_question"),
        (track_analyzed("click", outcome="FAILED"), "track_failed"),
    ],
)
async def test_spec025_r4_skipped_item_does_not_call_store_or_publish(
    message: Event, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="embedding_worker")
    storage = InMemoryStorage()

    gateway, _, publisher = await _run(message, storage=storage)

    assert gateway.requests == []
    assert publisher.published == []
    assert storage.delete_prefix(f"sources/{SOURCE}/") == 0
    [record] = [r for r in caplog.records if r.name.startswith("embedding_worker")]
    assert record.__dict__["reason"] == reason
    assert record.__dict__["inputEventId"] == str(message.event_id)


async def test_spec025_r4_skipped_track_does_not_read_mapping() -> None:
    # 번역표가 없어도 건너뛸 메시지는 재시도로 가지 않는다.
    _, _, publisher = await _run(track_analyzed("question"), storage=InMemoryStorage())

    assert publisher.published == []


# --- R-9, R-10 ai-gateway 오류 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_PROVIDER_UNAVAILABLE", 503, retryable=True),
        gateway_error("AI_CONNECTION_FAILED", None, retryable=True),
        gateway_error("AI_TIMEOUT", None, retryable=True),
        gateway_error("AI_RATE_LIMITED", 429),
    ],
)
async def test_spec025_r9_transient_failure_is_raised_for_retry_topic(
    error: AiGatewayError,
) -> None:
    with pytest.raises(AiGatewayError) as raised:
        await _run(event_analyzed(), FakeGateway(reply=lambda _: error))

    assert not isinstance(raised.value, NonRetryableError)


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_PROVIDER_FAILED", 502),
        gateway_error("AI_DEADLINE_EXCEEDED", 504),
        gateway_error("AI_HTTP_500", 500),
    ],
)
async def test_spec025_r9_other_5xx_is_raised_for_retry_topic(error: AiGatewayError) -> None:
    """D-14: R-9에 없는 502·504·그 밖의 5xx도 재시도 토픽으로 보낸다."""
    with pytest.raises(AiGatewayError):
        await _run(event_analyzed(), FakeGateway(reply=lambda _: error))


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_MODEL_NOT_CONFIGURED", 503, retryable=True),
        gateway_error("AI_BUDGET_EXCEEDED", 429),
    ],
)
async def test_spec025_r9_unconfigured_model_or_budget_goes_to_dlq(error: AiGatewayError) -> None:
    gateway = FakeGateway(reply=lambda _: error)

    with pytest.raises(NonRetryableError, match=error.code):
        await _run(event_analyzed(), gateway)


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_CALLER_FORBIDDEN", 403),
        gateway_error("AI_UNKNOWN_TASK_TYPE", 400),
        gateway_error("AI_HTTP_422", 422),
        gateway_error("AI_INVALID_RESPONSE", 200),
    ],
)
async def test_spec025_r10_rejected_call_goes_to_dlq_without_publish(error: AiGatewayError) -> None:
    publisher = RecordingPublisher()

    with pytest.raises(NonRetryableError, match=error.code):
        await _run(event_analyzed(), FakeGateway(reply=lambda _: error), publisher=publisher)

    assert publisher.published == []


def _response(vectors: list[list[float]], dimension: int = DIMENSION) -> EmbedResponse:
    return EmbedResponse(
        call_id=UUID(int=3),
        task_type="embed_text",
        model_id=MODEL,
        dimension=dimension,
        vectors=vectors,
    )


@pytest.mark.parametrize(
    "response",
    [
        _response([]),
        _response([unit_vector(), unit_vector()]),
        _response([[1.0, 0.0]]),
        _response([[1.0] * 5000], dimension=5000),
    ],
)
async def test_spec025_r10_unexpected_vectors_go_to_dlq_without_store_or_publish(
    response: EmbedResponse,
) -> None:
    storage = _storage()
    publisher = RecordingPublisher()

    with pytest.raises(NonRetryableError):
        await _run(
            event_analyzed(),
            FakeGateway(reply=lambda _: response),
            storage=storage,
            publisher=publisher,
        )

    assert publisher.published == []
    item = event_analyzed().event_id
    assert not storage.exists(f"sources/{SOURCE}/jobs/{JOB}/text-vectors/event/{item}.json")


# --- R-12 저장소·Kafka 일시 오류 ----------------------------------------------------------------


async def test_spec025_r12_storage_failure_is_raised_without_publish() -> None:
    publisher = RecordingPublisher()

    with pytest.raises(ConnectionError):
        await _run(event_analyzed(), storage=FailingStorage(), publisher=publisher)

    assert publisher.published == []


async def test_spec025_r12_missing_track_mapping_is_raised_for_retry() -> None:
    with pytest.raises(ObjectNotFoundError):
        await _run(track_analyzed(), storage=InMemoryStorage())


async def test_spec025_r12_publish_failure_is_raised_and_retry_rewrites_same_file_and_id() -> None:
    storage = _storage()
    failing = RecordingPublisher(fail_with=PublishError("broker unavailable"))

    with pytest.raises(PublishError):
        await _run(event_analyzed(), storage=storage, publisher=failing)
    first = storage.get_bytes(
        f"sources/{SOURCE}/jobs/{JOB}/text-vectors/event/{event_analyzed().event_id}.json"
    )
    _, _, publisher = await _run(event_analyzed(), storage=storage)

    event = _published(publisher)
    assert storage.get_bytes(event.vector_key) == first
    _, _, again = await _run(event_analyzed(), storage=_storage())
    assert _published(again).event_id == event.event_id


# --- NFR-2 로그 --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        None,
        lambda _: gateway_error("AI_MODEL_NOT_CONFIGURED", 503),
        lambda _: gateway_error("AI_PROVIDER_UNAVAILABLE", 503),
    ],
    ids=["ok", "permanent", "transient"],
)
async def test_spec025_nfr2_logs_have_no_text_or_vector(
    reply: Reply | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    gateway = FakeGateway(reply=reply) if reply is not None else FakeGateway()

    with contextlib.suppress(AiGatewayError, NonRetryableError):
        await _run(event_analyzed(), gateway)

    for record in caplog.records:
        rendered = record.getMessage() + json.dumps(
            record.__dict__, default=str, ensure_ascii=False
        )
        assert DESCRIPTION not in rendered
        assert "0.0, 0.0" not in rendered
