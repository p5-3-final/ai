"""실제 Kafka·SeaweedFS·Redis로 track-stitcher 워커 전체를 검증한다(SPEC-022 성공 지표, R-18, R-20).

구현 뒤에 쓰는 통합 테스트라 Red 단계가 없다(plan 6절).
"""

import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from confluent_kafka import Consumer
from stitcher_support import (
    PROFILE,
    boundary_message,
    crossing_chunks,
    embedded_message,
    event_errors,
    mapping_errors,
    store_chunk,
    store_vectors,
    vector_file,
)
from track_stitcher.__main__ import build
from track_stitcher.redis_state import RedisJobStateStore
from track_stitcher.settings import TrackStitcherSettings, WorkerSettings
from vlm_common.health import HealthState
from vlm_contracts.events import TrackDetected, TrackManifest
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = pytest.mark.integration

BUCKET = "vlm-media"
GROUP = "track-stitcher"
INPUTS = ["chunk.boundary", "segment.embedded", "video.deleted"]
TOPICS = [
    *INPUTS,
    *(f"{t}.{GROUP}.retry" for t in INPUTS),
    f"{GROUP}.dlq",
    "track.detected",
    "track.manifest",
]
LOCAL = Path(__file__).parents[1] / "profiles" / "local.toml"


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        containers.create_topics(address, TOPICS)
        yield address


@pytest.fixture(scope="module")
def storage(monkeypatch_module: pytest.MonkeyPatch) -> Iterator[S3Storage]:
    with containers.seaweedfs() as endpoint:
        containers.create_bucket(endpoint, BUCKET)
        monkeypatch_module.setenv("AWS_ACCESS_KEY_ID", containers.TEST_ACCESS_KEY)
        monkeypatch_module.setenv("AWS_SECRET_ACCESS_KEY", containers.TEST_ACCESS_KEY)
        yield S3Storage(StorageSettings(bucket=BUCKET, endpoint_url=endpoint, path_style=True))


@pytest.fixture(scope="module")
def redis_url() -> Iterator[str]:
    with containers.redis() as url:
        yield url


@pytest.fixture(scope="module")
def running(bootstrap: str, storage: S3Storage, redis_url: str) -> Iterator[HealthState]:
    health = HealthState()
    settings = TrackStitcherSettings(profile_path=LOCAL, redis_url=redis_url)
    worker, _ = build(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        storage,
        RedisJobStateStore(redis_url),
        PROFILE,
        health,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


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


def test_worker_becomes_ready(running: HealthState) -> None:
    for _ in range(80):
        if running.is_ready():
            return
        threading.Event().wait(0.25)
    raise AssertionError("worker not ready")


def test_spec022_success_five_chunks_give_four_pairs_and_one_manifest(
    running: HealthState, bootstrap: str, storage: S3Storage
) -> None:
    source = uuid.uuid4()
    files = crossing_chunks(source=source)
    publisher = KafkaPublisher(bootstrap=bootstrap, client_id="test")
    for f in files:
        vf = vector_file(f)
        store_chunk(storage, f)
        store_vectors(storage, vf)
        publisher.publish("chunk.boundary", boundary_message(f))
        publisher.publish("segment.embedded", embedded_message(vf))

    detected = _collect(bootstrap, "track.detected", source, 4, timeout_s=90)
    [manifest] = _collect(bootstrap, "track.manifest", source, 1, timeout_s=90)

    for raw in detected:
        assert event_errors(TrackDetected.model_validate(raw)) == []
    parsed = TrackManifest.model_validate(manifest)
    assert event_errors(parsed) == []
    assert (parsed.complete, parsed.chunk_count, parsed.track_count) == (True, 5, 1)
    assert mapping_errors(storage.get_bytes(parsed.mapping_key)) == []


def test_spec022_r18_invalid_message_and_missing_file_go_to_dlq(
    running: HealthState, bootstrap: str
) -> None:
    source = uuid.uuid4()
    [first, second, *_] = crossing_chunks(source=source)
    publisher = KafkaPublisher(bootstrap=bootstrap, client_id="test")
    invalid = json.loads(boundary_message(first).model_dump_json(by_alias=True))
    invalid["trackCount"] = -1
    publisher.send_raw("chunk.boundary", str(source), json.dumps(invalid).encode(), [])
    # 트랙 파일을 올리지 않은 메시지
    publisher.publish("chunk.boundary", boundary_message(second))

    dead = _collect(bootstrap, f"{GROUP}.dlq", source, 2, timeout_s=90)

    assert {d["chunkId"] for d in dead} == {str(first.chunk_id), str(second.chunk_id)}
    _collect(bootstrap, "track.detected", source, 0, timeout_s=3)
