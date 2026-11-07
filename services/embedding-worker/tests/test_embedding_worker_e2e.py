"""실제 Kafka로 embedding-worker 워커 전체를 검증한다(SPEC-025 R-6, R-9, R-11, NFR-3).

ai-gateway는 MockTransport, 저장소는 메모리 구현으로 대신한다.
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from confluent_kafka import Consumer, TopicPartition
from embedding_worker.__main__ import build_client, build_worker
from embedding_worker.settings import EmbeddingWorkerSettings, WorkerSettings
from ew_support import (
    DIMENSION,
    JOB,
    MODEL,
    SOURCE,
    event_analyzed,
    event_errors,
    mapping_key,
    segment_annotated,
    text_vector_file_errors,
    track_analyzed,
    track_mapping,
    unit_vector,
)
from vlm_common.health import HealthState
from vlm_contracts.events import EmbeddingReady, Event
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.memory import InMemoryStorage
from vlm_testing import containers

pytestmark = pytest.mark.integration

GROUP = "embedding-worker"
INPUTS = ["event.analyzed", "track.analyzed", "segment.annotated"]
# 이 문장을 받으면 가짜 ai-gateway가 해당 오류를 돌려준다.
NOT_CONFIGURED = "모델 미설정 문장"
UNAVAILABLE = "모델 일시 중단 문장"


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        retries = [f"{t}.{GROUP}.retry" for t in INPUTS]
        containers.create_topics(address, [*INPUTS, *retries, "embedding.ready", f"{GROUP}.dlq"])
        yield address


class _Gateway(httpx.AsyncBaseTransport):
    """문장에 따라 벡터나 오류를 돌려주는 가짜 ai-gateway. 문장별 호출 수를 센다."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        [text] = json.loads(request.content)["texts"]
        self.calls[text] = self.calls.get(text, 0) + 1
        problem = {"type": "about:blank", "title": "Service Unavailable", "status": 503}
        if text == NOT_CONFIGURED:
            return httpx.Response(503, json={**problem, "code": "AI_MODEL_NOT_CONFIGURED"})
        if text == UNAVAILABLE:
            return httpx.Response(503, json={**problem, "code": "AI_PROVIDER_UNAVAILABLE"})
        return httpx.Response(
            200,
            json={
                "callId": str(uuid.uuid4()),
                "taskType": "embed_text",
                "modelId": MODEL,
                "dimension": DIMENSION,
                "vectors": [unit_vector()],
            },
        )


@pytest.fixture(scope="module")
def gateway() -> _Gateway:
    return _Gateway()


@pytest.fixture(scope="module")
def storage() -> InMemoryStorage:
    return InMemoryStorage()


@pytest.fixture(scope="module")
def running(bootstrap: str, gateway: _Gateway, storage: InMemoryStorage) -> Iterator[HealthState]:
    health = HealthState()
    settings = EmbeddingWorkerSettings(
        ai_gateway_url="http://ai-gateway:8000", max_retries=2, retry_base_delay_s=0
    )
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        build_client(settings, transport=gateway),
        storage,
        health,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


def _body(message: Event) -> dict[str, object]:
    """테스트끼리 결과를 나누려고 영상·이벤트마다 다른 ID를 쓴다."""
    source = uuid.uuid4()
    text = message.model_dump_json(by_alias=True).replace(str(SOURCE), str(source))
    body: dict[str, object] = json.loads(text)
    return {**body, "eventId": str(uuid.uuid4())}


def _send(bootstrap: str, topic: str, body: dict[str, object]) -> None:
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        topic, str(body["sourceId"]), json.dumps(body).encode(), []
    )


def _collect(
    bootstrap: str, topic: str, source: object, count: int, timeout_s: float
) -> list[dict[str, object]]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"reader-{uuid.uuid4().hex}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    found: list[dict[str, object]] = []
    try:
        deadline = time.monotonic() + timeout_s
        # 0건을 기대하면 제한 시간 동안 끝까지 지켜본다.
        while time.monotonic() < deadline and (count == 0 or len(found) < count):
            msg = consumer.poll(0.5)
            if msg is None or msg.error() is not None:
                continue
            payload: dict[str, object] = json.loads(msg.value() or b"{}")
            if payload.get("sourceId") == str(source):
                found.append(payload)
    finally:
        consumer.close()
    assert len(found) == count, f"{topic} for {source}: {len(found)}/{count}"
    return found


def test_worker_becomes_ready(running: HealthState) -> None:
    for _ in range(40):
        if running.is_ready():
            return
        threading.Event().wait(0.25)
    raise AssertionError("worker not ready")


@pytest.mark.parametrize(
    ("topic", "message", "kind"),
    [
        ("event.analyzed", event_analyzed(), "event"),
        ("track.analyzed", track_analyzed(), "track"),
        ("segment.annotated", segment_annotated(), "segment_annotation"),
    ],
)
def test_spec025_us1_each_topic_becomes_embedding_ready_with_stored_file(
    running: HealthState,
    bootstrap: str,
    storage: InMemoryStorage,
    topic: str,
    message: Event,
    kind: str,
) -> None:
    body = _body(message)
    source = uuid.UUID(str(body["sourceId"]))
    if kind == "track":
        mapping = track_mapping().replace(str(SOURCE).encode(), str(source).encode())
        storage.put_bytes(mapping_key(source, JOB), mapping, "application/json")
    _send(bootstrap, topic, body)

    [published] = _collect(bootstrap, "embedding.ready", source, 1, timeout_s=60)

    event = EmbeddingReady.model_validate(published)
    assert event_errors(event) == []
    assert event.item_kind == kind
    assert text_vector_file_errors(storage.get_bytes(event.vector_key)) == []


def test_spec025_r11_invalid_message_goes_to_dlq(running: HealthState, bootstrap: str) -> None:
    body = {**_body(segment_annotated()), "score": 3}
    _send(bootstrap, "segment.annotated", body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", body["sourceId"], 1, timeout_s=60)

    assert dead["eventId"] == body["eventId"]
    _collect(bootstrap, "embedding.ready", body["sourceId"], 0, timeout_s=3)


def test_spec025_r9_model_not_configured_goes_to_dlq_without_worker_retry(
    running: HealthState, bootstrap: str, gateway: _Gateway
) -> None:
    body = _body(segment_annotated(NOT_CONFIGURED))
    _send(bootstrap, "segment.annotated", body)

    _collect(bootstrap, f"{GROUP}.dlq", body["sourceId"], 1, timeout_s=60)

    _collect(bootstrap, f"segment.annotated.{GROUP}.retry", body["sourceId"], 0, timeout_s=3)
    # C-6: 503이라 클라이언트가 2회 다시 부른 뒤 DLQ로 간다.
    assert gateway.calls[NOT_CONFIGURED] == 3


def test_spec025_r9_unavailable_model_goes_to_retry_topic(
    running: HealthState, bootstrap: str, gateway: _Gateway
) -> None:
    body = _body(segment_annotated(UNAVAILABLE))
    _send(bootstrap, "segment.annotated", body)

    [retried, *_] = _collect(
        bootstrap, f"segment.annotated.{GROUP}.retry", body["sourceId"], 1, timeout_s=60
    )

    assert retried["eventId"] == body["eventId"]
    assert gateway.calls[UNAVAILABLE] >= 3


def test_spec025_nfr3_input_is_committed_after_publish(
    running: HealthState, bootstrap: str
) -> None:
    body = _body(event_analyzed())
    _send(bootstrap, "event.analyzed", body)

    _collect(bootstrap, "embedding.ready", body["sourceId"], 1, timeout_s=60)

    assert _wait_until(lambda: _committed(bootstrap, "event.analyzed") == _end(bootstrap), 15)


def _committed(bootstrap: str, topic: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": GROUP})
    try:
        [tp] = consumer.committed([TopicPartition(topic, 0)], timeout=10)
        return int(tp.offset)
    finally:
        consumer.close()


def _end(bootstrap: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": f"w-{uuid.uuid4().hex}"})
    try:
        _, high = consumer.get_watermark_offsets(TopicPartition("event.analyzed", 0), timeout=10)
        return int(high)
    finally:
        consumer.close()


def _wait_until(condition: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return False
