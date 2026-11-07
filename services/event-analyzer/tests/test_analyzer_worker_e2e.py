"""실제 Kafka로 event-analyzer 워커 전체를 검증한다. ai-gateway는 MockTransport로 대신한다."""

import asyncio
import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from confluent_kafka import Consumer, TopicPartition
from ea_support import (
    PROFILE_PATH,
    alert_candidate,
    candidate,
    candidates_message,
    detected,
    detector_message,
    file_message,
    judged,
)
from event_analyzer.__main__ import Dependencies, build_client, build_worker
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rejudge_state import NoRejudgeState
from event_analyzer.settings import EventAnalyzerSettings, WorkerSettings
from vlm_common.health import HealthState
from vlm_common.profile import load_profile
from vlm_contracts.events import (
    AlertRaised,
    Event,
    EventAnalyzed,
    EventCandidateVector,
    EventManifest,
)
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.memory import InMemoryStorage
from vlm_testing import containers

pytestmark = pytest.mark.integration

TOPIC = "event.candidate.vector"
GROUP = "event-analyzer"
# NFR-2 확인용: 그룹 하트비트 기준보다 오래 걸리는 판정
SLOW_GROUP = "event-analyzer-slow"
POLL_INTERVAL_MS = 6000
# ai-gateway가 호출자를 거절하는 영상. 그 영상의 메시지는 DLQ로 가야 한다(R-15).
FORBIDDEN_SOURCE = uuid.UUID("0192f0d2-0000-7000-8000-00000000f403")


DETECTOR_TOPIC = "event.candidate"
ALERT_TOPIC = "alert.candidate"


def _topics(group: str) -> list[str]:
    retries = [f"{t}.{group}.retry" for t in (TOPIC, DETECTOR_TOPIC, ALERT_TOPIC)]
    return [*retries, f"{group}.dlq"]


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        containers.create_topics(
            address,
            [
                TOPIC,
                DETECTOR_TOPIC,
                ALERT_TOPIC,
                "event.analyzed",
                "event.manifest",
                "alert.raised",
                *_topics(GROUP),
                *_topics(SLOW_GROUP),
            ],
        )
        yield address


def _gateway(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    if body["sourceId"] == str(FORBIDDEN_SOURCE):
        return httpx.Response(403, json={"status": 403, "code": "AI_CALLER_FORBIDDEN"})
    return httpx.Response(
        200,
        json={
            "callId": str(uuid.uuid4()),
            "taskType": body["taskType"],
            "modelId": "mock",
            "promptVersion": f"{body['taskType']}@0123456789ab",
            "taxonomyVersion": "v2",
            "output": judged(body["variables"]["eventType"]),
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
    settings = EventAnalyzerSettings(ai_gateway_url="http://ai-gateway:8000", group_id=group_id)
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        build_client(settings, transport=transport),
        health,
        Dependencies(
            storage=InMemoryStorage(),
            profile=load_profile(PROFILE_PATH, EventAnalyzerProfile),
            rejudge=NoRejudgeState(),
        ),
        kafka=kafka,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def running(bootstrap: str) -> Iterator[HealthState]:
    yield from _start(bootstrap, httpx.MockTransport(_gateway))


def _reissued[M: Event](message: M, source: uuid.UUID | None = None) -> M:
    """테스트끼리 결과를 나누려고 영상·메시지마다 다른 ID를 쓴다. 이미지 경로도 그 영상 폴더로
    바꾼다."""
    source_id = source or uuid.uuid4()
    body = json.loads(message.model_dump_json(by_alias=True))
    text = json.dumps({**body, "eventId": str(uuid.uuid4())}).replace(
        str(message.source_id), str(source_id)
    )
    return type(message).model_validate_json(text)


def _message(count: int = 2, source: uuid.UUID | None = None) -> EventCandidateVector:
    return _reissued(candidates_message(*(candidate(i) for i in range(count))), source)


def _publish(bootstrap: str, message: Event, topic: str = TOPIC) -> None:
    KafkaPublisher(bootstrap=bootstrap, client_id="test").publish(topic, message)


def _send_raw(bootstrap: str, topic: str, message: Event, body: dict[str, object]) -> None:
    source = str(message.source_id)
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        topic, source, json.dumps(body).encode(), []
    )


def _collect(
    bootstrap: str, topic: str, source: uuid.UUID, count: int, timeout_s: float
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


def test_spec020_us1_candidates_become_event_analyzed(running: HealthState, bootstrap: str) -> None:
    message = _message(count=2)
    _publish(bootstrap, message)

    published = _collect(bootstrap, "event.analyzed", message.source_id, 2, timeout_s=60)

    events = [EventAnalyzed.model_validate(p) for p in published]
    assert {e.candidate_id for e in events} == {c.candidate_id for c in message.candidates}
    assert {e.judgment_method for e in events} == {"vector+vlm"}


def test_spec020_r16_invalid_message_goes_to_dlq(running: HealthState, bootstrap: str) -> None:
    message = _message(count=1)
    body = json.loads(message.model_dump_json(by_alias=True))
    body["candidates"][0]["frameKeys"] = body["candidates"][0]["frameKeys"][:3]
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        TOPIC, str(message.source_id), json.dumps(body).encode(), []
    )

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", message.source_id, 1, timeout_s=60)

    assert dead["eventId"] == str(message.event_id)


def test_spec020_r15_rejected_caller_goes_to_dlq(running: HealthState, bootstrap: str) -> None:
    message = _message(count=2, source=FORBIDDEN_SOURCE)
    _publish(bootstrap, message)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", FORBIDDEN_SOURCE, 1, timeout_s=60)

    assert dead["eventId"] == str(message.event_id)
    # DLQ로 간 메시지는 후보를 하나도 발행하지 않는다.
    _collect(bootstrap, "event.analyzed", FORBIDDEN_SOURCE, 0, timeout_s=3)


class _SlowGateway(httpx.AsyncBaseTransport):
    """대상 영상의 판정을 그룹의 `max.poll.interval.ms`보다 오래 끄는 가짜 ai-gateway.

    새 소비 그룹은 앞선 테스트의 메시지도 처음부터 읽으므로, 대상 영상의 호출만 센다.
    """

    def __init__(self, target: uuid.UUID) -> None:
        self._target = str(target)
        self.started = 0
        self.finished = threading.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["sourceId"] != self._target:
            return _gateway(request)
        self.started += 1
        await asyncio.sleep(POLL_INTERVAL_MS / 1000 + 2)
        self.finished.set()
        return _gateway(request)


def test_spec020_nfr1_nfr2_slow_judgment_is_committed_once_without_rebalance(
    bootstrap: str,
) -> None:
    message = _message(count=1)
    slow = _SlowGateway(message.source_id)
    kafka = {"max.poll.interval.ms": str(POLL_INTERVAL_MS), "session.timeout.ms": "6000"}
    runner = _start(bootstrap, slow, group_id=SLOW_GROUP, kafka=kafka)
    next(runner)
    try:
        _publish(bootstrap, message)

        assert slow.finished.wait(timeout=60)
        # 그룹에서 빠졌다면 처리 결과를 커밋하지 못한다(libs/messaging의 같은 검사).
        assert _wait_until(lambda: _committed(bootstrap, SLOW_GROUP) == _end(bootstrap), 15)
        assert slow.started == 1
        _collect(bootstrap, "event.analyzed", message.source_id, 1, timeout_s=30)
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


def test_spec023_us1_us2_detector_candidates_become_analyzed_and_manifest(
    running: HealthState, bootstrap: str
) -> None:
    message = _reissued(
        detector_message(
            detected(1, "ppe_missing"),
            detected(2, "hazard_zone_access", judgment="rule", track="t2", zone_name="A"),
        )
    )
    _publish(bootstrap, message, DETECTOR_TOPIC)

    analyzed = _collect(bootstrap, "event.analyzed", message.source_id, 2, timeout_s=60)
    [manifest] = _collect(bootstrap, "event.manifest", message.source_id, 1, timeout_s=30)

    events = [EventAnalyzed.model_validate(p) for p in analyzed]
    assert {e.judgment_method for e in events} == {"rule+vlm", "rule"}
    parsed = EventManifest.model_validate(manifest)
    assert set(parsed.analyzed_event_ids) == {e.event_id for e in events}


def test_spec023_r14_invalid_detector_message_goes_to_dlq(
    running: HealthState, bootstrap: str
) -> None:
    message = _reissued(detector_message(detected(1)))
    body = json.loads(message.model_dump_json(by_alias=True))
    body["candidateCount"] = 5
    _send_raw(bootstrap, DETECTOR_TOPIC, message, body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", message.source_id, 1, timeout_s=60)

    assert dead["eventId"] == str(message.event_id)


def test_spec023_r2_missing_candidate_file_goes_to_dlq_without_output(
    running: HealthState, bootstrap: str
) -> None:
    message = _reissued(file_message(detected(1)))
    _publish(bootstrap, message, DETECTOR_TOPIC)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", message.source_id, 1, timeout_s=60)

    assert dead["eventId"] == str(message.event_id)
    _collect(bootstrap, "event.manifest", message.source_id, 0, timeout_s=3)


def test_spec023_r21_alert_candidate_becomes_pending_then_confirmed(
    running: HealthState, bootstrap: str
) -> None:
    message = _reissued(alert_candidate("fall"))
    _publish(bootstrap, message, ALERT_TOPIC)

    raised = _collect(bootstrap, "alert.raised", message.source_id, 2, timeout_s=60)

    statuses = [AlertRaised.model_validate(r).status for r in raised]
    assert statuses == ["pending", "confirmed"]
    _collect(bootstrap, "event.analyzed", message.source_id, 0, timeout_s=3)


def test_spec023_r21_invalid_alert_candidate_goes_to_dlq(
    running: HealthState, bootstrap: str
) -> None:
    message = _reissued(alert_candidate())
    body = json.loads(message.model_dump_json(by_alias=True))
    body["jobType"] = "FULL"
    _send_raw(bootstrap, ALERT_TOPIC, message, body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", message.source_id, 1, timeout_s=60)

    assert dead["eventId"] == str(message.event_id)
