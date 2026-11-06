"""실제 Kafka로 track-analyzer 워커 전체를 검증한다. ai-gateway는 MockTransport로 대신한다."""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from confluent_kafka import Consumer, TopicPartition
from ta_support import ANSWER, ATTRIBUTES, SOURCE, attribute_request, event_errors
from track_analyzer.__main__ import build_client, build_worker
from track_analyzer.settings import TrackAnalyzerSettings, WorkerSettings
from vlm_common.health import HealthState
from vlm_contracts.events import AttributeReason, TrackAnalyzed
from vlm_messaging.publisher import KafkaPublisher
from vlm_testing import containers

pytestmark = pytest.mark.integration

TOPIC = "attribute.requested"
GROUP = "track-analyzer"
# NFR-2 확인용: 그룹 하트비트 기준보다 오래 걸리는 호출
SLOW_GROUP = "track-analyzer-slow"
POLL_INTERVAL_MS = 6000
# ai-gateway가 503을 돌려주는 영상. 그 영상의 요청은 FAILED로 발행돼야 한다(R-4).
UNAVAILABLE_SOURCE = uuid.UUID("0192f0d2-0000-7000-8000-000000000503")


def _topics(group: str) -> list[str]:
    return [f"{TOPIC}.{group}.retry", f"{group}.dlq"]


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        containers.create_topics(
            address, [TOPIC, "track.analyzed", *_topics(GROUP), *_topics(SLOW_GROUP)]
        )
        yield address


class _Gateway(httpx.AsyncBaseTransport):
    """taskType에 맞는 결과를 돌려주는 가짜 ai-gateway. 영상별 호출 수를 센다."""

    def __init__(self, slow_source: uuid.UUID | None = None) -> None:
        self.calls: dict[str, int] = {}
        self._slow = str(slow_source) if slow_source else None
        self.finished = threading.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        source = body["sourceId"]
        self.calls[source] = self.calls.get(source, 0) + 1
        if source == self._slow:
            await asyncio.sleep(POLL_INTERVAL_MS / 1000 + 2)
            self.finished.set()
        if source == str(UNAVAILABLE_SOURCE):
            problem = {"type": "about:blank", "title": "Service Unavailable", "status": 503}
            return httpx.Response(503, json={**problem, "code": "AI_PROVIDER_UNAVAILABLE"})
        task_type = body["taskType"]
        return httpx.Response(
            200,
            json={
                "callId": str(uuid.uuid4()),
                "taskType": task_type,
                "modelId": "mock",
                "promptVersion": f"{task_type}@0123456789ab",
                "taxonomyVersion": "v2",
                "output": ATTRIBUTES if task_type == "track_attribute" else ANSWER,
            },
        )


def _start(
    bootstrap: str,
    transport: httpx.AsyncBaseTransport,
    *,
    group_id: str = GROUP,
    kafka: dict[str, str] | None = None,
) -> Iterator[HealthState]:
    health = HealthState()
    settings = TrackAnalyzerSettings(ai_gateway_url="http://ai-gateway:8000", group_id=group_id)
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        build_client(settings, transport=transport),
        health,
        kafka=kafka,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def gateway() -> _Gateway:
    return _Gateway()


@pytest.fixture(scope="module")
def running(bootstrap: str, gateway: _Gateway) -> Iterator[HealthState]:
    yield from _start(bootstrap, gateway)


def _body(reason: AttributeReason = "click", source: uuid.UUID | None = None) -> dict[str, object]:
    """테스트끼리 결과를 나누려고 영상·요청마다 다른 ID를 쓴다. 이미지 경로도 그 영상 폴더다."""
    message = attribute_request(reason, scenes=2 if reason == "question" else 0)
    text = message.model_dump_json(by_alias=True).replace(str(SOURCE), str(source or uuid.uuid4()))
    body: dict[str, object] = json.loads(text)
    return {**body, "eventId": str(uuid.uuid4()), "requestId": str(uuid.uuid4())}


def _send(bootstrap: str, body: dict[str, object]) -> None:
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        TOPIC, str(body["sourceId"]), json.dumps(body).encode(), []
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


@pytest.mark.parametrize("reason", ["click", "question"])
def test_spec024_us1_us2_request_becomes_track_analyzed(
    running: HealthState, bootstrap: str, reason: AttributeReason
) -> None:
    body = _body(reason)
    _send(bootstrap, body)

    [published] = _collect(bootstrap, "track.analyzed", body["sourceId"], 1, timeout_s=60)

    event = TrackAnalyzed.model_validate(published)
    assert event_errors(event) == []
    assert (str(event.request_id), event.reason, event.outcome) == (
        body["requestId"],
        reason,
        "READY",
    )


def test_spec024_r4_unavailable_gateway_is_published_failed(
    running: HealthState, bootstrap: str
) -> None:
    _send(bootstrap, _body("click", source=UNAVAILABLE_SOURCE))

    [published] = _collect(bootstrap, "track.analyzed", UNAVAILABLE_SOURCE, 1, timeout_s=60)

    assert (published["outcome"], published["errorCode"]) == ("FAILED", "AI_PROVIDER_UNAVAILABLE")


def test_spec024_r7_invalid_message_goes_to_dlq(running: HealthState, bootstrap: str) -> None:
    body = {**_body("click"), "question": "클릭 요청에는 질문이 없다"}
    _send(bootstrap, body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", body["sourceId"], 1, timeout_s=60)

    assert dead["eventId"] == body["eventId"]
    _collect(bootstrap, "track.analyzed", body["sourceId"], 0, timeout_s=3)


def test_spec024_r6_image_outside_source_goes_to_dlq_without_call(
    running: HealthState, bootstrap: str, gateway: _Gateway
) -> None:
    body = _body("click")
    crops = body["crops"]
    assert isinstance(crops, list)
    crops[0]["key"] = f"sources/{uuid.uuid4()}/jobs/j/blurred/other.jpg"
    _send(bootstrap, body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", body["sourceId"], 1, timeout_s=60)

    assert dead["eventId"] == body["eventId"]
    assert gateway.calls.get(str(body["sourceId"]), 0) == 0
    _collect(bootstrap, "track.analyzed", body["sourceId"], 0, timeout_s=3)


def test_spec024_nfr2_slow_call_is_committed_once_without_rebalance(bootstrap: str) -> None:
    body = _body("click")
    source = uuid.UUID(str(body["sourceId"]))
    slow = _Gateway(slow_source=source)
    kafka = {"max.poll.interval.ms": str(POLL_INTERVAL_MS), "session.timeout.ms": "6000"}
    runner = _start(bootstrap, slow, group_id=SLOW_GROUP, kafka=kafka)
    next(runner)
    try:
        _send(bootstrap, body)

        assert slow.finished.wait(timeout=60)
        # 그룹에서 빠졌다면 처리 결과를 커밋하지 못한다(libs/messaging의 같은 검사).
        assert _wait_until(lambda: _committed(bootstrap, SLOW_GROUP) == _end(bootstrap), 15)
        assert slow.calls[str(source)] == 1
        _collect(bootstrap, "track.analyzed", source, 1, timeout_s=30)
    finally:
        next(runner, None)


def _committed(bootstrap: str, group_id: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group_id})
    try:
        [tp] = consumer.committed([TopicPartition(TOPIC, 0)], timeout=10)
        return int(tp.offset)
    finally:
        consumer.close()


def _end(bootstrap: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": f"w-{uuid.uuid4().hex}"})
    try:
        _, high = consumer.get_watermark_offsets(TopicPartition(TOPIC, 0), timeout=10)
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
