"""실제 Kafka로 aggregator 워커 전체를 검증한다(SPEC-027 R-1, R-22, R-23, R-30, R-32, NFR-4).

analysis-api·ai-gateway·stream-service는 MockTransport, 저장소는 메모리, Redis는 메모리 구현으로
대신한다.
"""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator

import httpx
import pytest
from agg_support import (
    FALL_STREAM,
    STREAM_JOB,
    STREAM_SOURCE,
    UP_STREAM,
    ZONES,
    completed,
    event,
    event_errors,
    job_event_json,
    risk_event,
    stream_analyzed,
    stream_completed,
    stream_manifest,
)
from aggregator.__main__ import Transports, build_clients, build_worker
from aggregator.kv import MemoryStore
from aggregator.settings import AggregatorSettings, WorkerSettings
from confluent_kafka import Consumer
from vlm_common.health import HealthState
from vlm_contracts.aggregates import result_key
from vlm_contracts.events import Event, SourceCompleted, StateUpdated
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.memory import InMemoryStorage
from vlm_testing import containers

pytestmark = pytest.mark.integration

GROUP = "aggregator"
CONSUMED = ["analysis.completed", "event.analyzed", "track.detected", "chunk.manifest"]
# analysis-api가 400·503으로 답하는 영상
BAD_REQUEST_SOURCE = uuid.UUID("0192f0d2-0000-7000-8000-00000000f400")
UNAVAILABLE_SOURCE = uuid.UUID("0192f0d2-0000-7000-8000-00000000f503")
EVENTS = [job_event_json(risk_event(1, 30_000, detected=True)), job_event_json(event(2, start=0))]


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        topics = [*CONSUMED, "source.completed", "state.updated", f"{GROUP}.dlq"]
        topics += [f"{t}.{GROUP}.retry" for t in CONSUMED]
        containers.create_topics(address, topics)
        yield address


def _analysis_api(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if str(BAD_REQUEST_SOURCE) in path:
        return httpx.Response(
            400,
            json={
                "type": "about:blank",
                "title": "x",
                "status": 400,
                "code": "ANALYSIS_INVALID_REQUEST",
            },
        )
    if str(UNAVAILABLE_SOURCE) in path:
        return httpx.Response(
            503,
            json={
                "type": "about:blank",
                "title": "x",
                "status": 503,
                "code": "ANALYSIS_UNAVAILABLE",
            },
        )
    if path.endswith("/final-state"):
        problem = {"type": "about:blank", "title": "x", "status": 404, "code": "NOT_FOUND"}
        return httpx.Response(404, json=problem)
    if path.endswith("/zones"):
        return httpx.Response(
            200,
            json={
                "items": [{"zoneId": str(z.zone_id), "name": z.name, "kind": z.kind} for z in ZONES]
            },
        )
    if path.endswith("/events"):
        return httpx.Response(200, json={"items": EVENTS, "nextCursor": None})
    return httpx.Response(200, json={"items": [], "nextCursor": None})


def _gateway(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "callId": str(uuid.uuid4()),
            "taskType": body["taskType"],
            "modelId": "mock",
            "promptVersion": f"{body['taskType']}@0123456789ab",
            "taxonomyVersion": None,
            "output": {"summary": "요약", "highlights": []},
        },
    )


def _stream_service(request: httpx.Request) -> httpx.Response:
    zones = [
        {
            "zoneId": str(z.zone_id),
            "name": z.name,
            "kind": z.kind,
            "polygon": [[0, 0], [1, 0], [1, 1]],
        }
        for z in ZONES
    ]
    return httpx.Response(200, json={"zones": zones, "updatedAt": None})


class _Running:
    def __init__(self, health: HealthState, storage: InMemoryStorage, store: MemoryStore) -> None:
        self.health = health
        self.storage = storage
        self.store = store


@pytest.fixture(scope="module")
def running(bootstrap: str) -> Iterator[_Running]:
    health, storage, store = HealthState(), InMemoryStorage(), MemoryStore()
    settings = AggregatorSettings(
        ai_gateway_url="http://ai-gateway:8000",
        analysis_api_url="http://analysis-api:8085",
        stream_service_url="http://stream-service:8080",
        retry_base_delay_s=0,
        max_attempts=2,
    )
    transports = Transports(
        gateway=httpx.MockTransport(_gateway),
        analysis=httpx.MockTransport(_analysis_api),
        stream=httpx.MockTransport(_stream_service),
    )
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        storage,
        build_clients(settings, transports),
        health,
        store=store,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield _Running(health, storage, store)
    worker.stop()
    thread.join(timeout=15)


def _publish(bootstrap: str, topic: str, message: Event) -> None:
    KafkaPublisher(bootstrap=bootstrap, client_id="test").publish(topic, message)


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


def _wait_until(condition: Callable[[], bool], timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.2)
    return False


def _pending_filled(store: MemoryStore, chunk_id: uuid.UUID | None) -> Callable[[], bool]:
    """창의 대기 집합에 이벤트 1건과 manifest가 모두 들어왔는지."""
    key = f"pending:{STREAM_SOURCE}:{chunk_id}"
    return lambda: len(store.hashes.get(key, {})) == 2


def test_worker_becomes_ready(running: _Running) -> None:
    assert _wait_until(running.health.is_ready, 10)


def test_spec027_r1_nfr4_completed_job_is_published_with_stored_result(
    running: _Running, bootstrap: str
) -> None:
    """[SPEC-027 R-1·NFR-4] analysis.completed가 결과 파일과 source.completed 한 건으로 이어진다."""
    source = uuid.uuid4()
    _publish(bootstrap, "analysis.completed", completed(source=source, events=2))

    [payload] = _collect(bootstrap, "source.completed", source, 1, timeout_s=60)

    sent = SourceCompleted.model_validate(payload)
    assert event_errors(sent, "source.completed") == []
    assert running.storage.exists(result_key(source, sent.job_id))


def test_spec027_r23_invalid_message_goes_to_dlq(running: _Running, bootstrap: str) -> None:
    source = uuid.uuid4()
    body = json.loads(completed(source=source).model_dump_json(by_alias=True))
    del body["counts"]
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        "analysis.completed", str(source), json.dumps(body).encode(), []
    )

    _collect(bootstrap, f"{GROUP}.dlq", source, 1, timeout_s=60)


def test_spec027_r23_bad_request_goes_to_dlq(running: _Running, bootstrap: str) -> None:
    _publish(bootstrap, "analysis.completed", completed(source=BAD_REQUEST_SOURCE))

    _collect(bootstrap, f"{GROUP}.dlq", BAD_REQUEST_SOURCE, 1, timeout_s=60)
    _collect(bootstrap, "source.completed", BAD_REQUEST_SOURCE, 0, timeout_s=3)


def test_spec027_r22_unavailable_api_goes_to_retry_topic(running: _Running, bootstrap: str) -> None:
    _publish(bootstrap, "analysis.completed", completed(source=UNAVAILABLE_SOURCE))

    _collect(bootstrap, f"analysis.completed.{GROUP}.retry", UNAVAILABLE_SOURCE, 1, timeout_s=60)


def test_spec027_r30_r32_r34_stream_windows_then_session(running: _Running, bootstrap: str) -> None:
    """[SPEC-027 R-30·R-32·R-34] 창 2개 → state.updated 2건, 세션 완료 → source.completed 1건."""
    windows = [
        (1, stream_analyzed(1, 1, at=12_000, result=FALL_STREAM)),
        (2, stream_analyzed(2, 2, at=33_000, result=UP_STREAM)),
    ]
    for sequence, analyzed in windows:
        _publish(bootstrap, "event.analyzed", analyzed)
        _publish(bootstrap, "chunk.manifest", stream_manifest(sequence))
        assert _wait_until(_pending_filled(running.store, analyzed.chunk_id), 30)
        _publish(bootstrap, "analysis.completed", stream_completed(sequence, events=1))
        _collect(bootstrap, "state.updated", STREAM_SOURCE, sequence, timeout_s=60)
    _publish(bootstrap, "analysis.completed", stream_completed(None))

    updates = [
        StateUpdated.model_validate(p)
        for p in _collect(bootstrap, "state.updated", STREAM_SOURCE, 2, timeout_s=5)
    ]
    [done] = _collect(bootstrap, "source.completed", STREAM_SOURCE, 1, timeout_s=60)

    assert [u.sequence for u in updates] == [1, 2]
    assert all(event_errors(u, "state.updated") == [] for u in updates)
    sent = SourceCompleted.model_validate(done)
    assert (sent.job_id, sent.revision) == (STREAM_JOB, 1)
