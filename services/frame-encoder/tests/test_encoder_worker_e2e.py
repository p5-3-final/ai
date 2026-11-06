"""실제 Kafka·SeaweedFS로 frame-encoder 워커 전체를 검증한다."""

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from confluent_kafka import Consumer, TopicPartition
from fe_support import encoder, image_bytes, put_images, segment_ready
from frame_encoder.__main__ import Prepared, build_worker
from frame_encoder.ids import vectors_key
from frame_encoder.service import EncoderConfig
from frame_encoder.settings import FrameEncoderSettings, WorkerSettings
from vlm_common.health import HealthState
from vlm_contracts.events import SegmentReady
from vlm_contracts.vectors import VectorFile
from vlm_encoders.base import Vector
from vlm_encoders.mock import MockEncoder
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = pytest.mark.integration

BUCKET = "vlm-media"
TOPICS = [
    "segment.ready",
    "segment.ready.frame-encoder.retry",
    "frame-encoder.dlq",
    "segment.embedded",
    "event.candidate.vector",
]
# NFR-3 확인용: 그룹 하트비트 기준보다 오래 걸리는 처리
SLOW_GROUP = "frame-encoder-slow"
POLL_INTERVAL_MS = 6000


class SlowEncoder(MockEncoder):
    """대상 이미지를 인코딩할 때 그룹의 `max.poll.interval.ms`보다 오래 걸리는 인코더.

    새 소비 그룹은 앞선 테스트의 메시지도 처음부터 읽으므로, 대상 이미지가 든 호출만 센다.
    """

    def __init__(self, target: bytes) -> None:
        super().__init__("mock:mock:mock:mean4", 8)
        self._target = target
        self.started = 0
        self.finished = threading.Event()

    def encode_images(self, images: list[bytes]) -> list[Vector]:
        if self._target not in images:
            return super().encode_images(images)
        self.started += 1
        time.sleep(POLL_INTERVAL_MS / 1000 + 2)
        vectors = super().encode_images(images)
        self.finished.set()
        return vectors


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        containers.create_topics(
            address,
            [*TOPICS, f"segment.ready.{SLOW_GROUP}.retry", f"{SLOW_GROUP}.dlq"],
        )
        yield address


@pytest.fixture(scope="module")
def storage(monkeypatch_module: pytest.MonkeyPatch) -> Iterator[S3Storage]:
    with containers.seaweedfs() as endpoint:
        containers.create_bucket(endpoint, BUCKET)
        monkeypatch_module.setenv("AWS_ACCESS_KEY_ID", containers.TEST_ACCESS_KEY)
        monkeypatch_module.setenv("AWS_SECRET_ACCESS_KEY", containers.TEST_ACCESS_KEY)
        yield S3Storage(StorageSettings(bucket=BUCKET, endpoint_url=endpoint, path_style=True))


def _start(
    bootstrap: str,
    storage: S3Storage,
    prepared: Prepared,
    *,
    group_id: str = "frame-encoder",
    kafka: dict[str, str] | None = None,
) -> Iterator[HealthState]:
    health = HealthState()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        FrameEncoderSettings(profile_path=Path("unused.toml"), group_id=group_id),
        storage,
        prepared,
        health,
        kafka=kafka,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def running(bootstrap: str, storage: S3Storage) -> Iterator[HealthState]:
    yield from _start(bootstrap, storage, Prepared(encoder(), EncoderConfig(batch_size=4)))


def _ready(storage: S3Storage, segments: int = 2, crops: int = 2) -> SegmentReady:
    ready = segment_ready(
        segments, crops, source=uuid.uuid4(), execution=uuid.uuid4(), event_id=uuid.uuid4()
    )
    put_images(storage, ready)
    return ready


def _publish(bootstrap: str, ready: SegmentReady) -> None:
    KafkaPublisher(bootstrap=bootstrap, client_id="test").publish("segment.ready", ready)


def _wait_for(bootstrap: str, topic: str, source: uuid.UUID, timeout_s: float) -> dict[str, object]:
    consumer = Consumer(
        {
            "bootstrap.servers": bootstrap,
            "group.id": f"reader-{uuid.uuid4().hex}",
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([topic])
    try:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            msg = consumer.poll(0.5)
            if msg is None or msg.error() is not None:
                continue
            payload: dict[str, object] = json.loads(msg.value() or b"{}")
            if payload.get("sourceId") == str(source):
                return payload
    finally:
        consumer.close()
    raise AssertionError(f"{topic} for {source} not received")


def test_worker_becomes_ready(running: HealthState) -> None:
    for _ in range(40):
        if running.is_ready():
            return
        threading.Event().wait(0.25)
    raise AssertionError("worker not ready")


def test_spec_019_segment_ready_becomes_vector_file_and_segment_embedded(
    running: HealthState, bootstrap: str, storage: S3Storage
) -> None:
    ready = _ready(storage)
    _publish(bootstrap, ready)

    embedded = _wait_for(bootstrap, "segment.embedded", ready.source_id, timeout_s=60)

    assert embedded["vectorsKey"] == vectors_key(ready)
    assert (embedded["segmentCount"], embedded["cropCount"]) == (2, 2)
    stored = VectorFile.model_validate_json(storage.get_bytes(vectors_key(ready)))
    assert stored.execution_id == ready.execution_id


def test_spec_019_r16_image_outside_execution_folder_goes_to_dlq(
    running: HealthState, bootstrap: str, storage: S3Storage
) -> None:
    ready = _ready(storage)
    other = _ready(storage)
    # 다른 영상의 이미지를 가리키는 메시지. 모델 검증을 거치지 않게 JSON을 직접 고친다.
    body = json.loads(ready.model_dump_json(by_alias=True))
    body["crops"][0]["key"] = other.crops[0].key
    KafkaPublisher(bootstrap=bootstrap, client_id="test").send_raw(
        "segment.ready", str(ready.source_id), json.dumps(body).encode(), []
    )

    dead = _wait_for(bootstrap, "frame-encoder.dlq", ready.source_id, timeout_s=60)

    assert dead["executionId"] == str(ready.execution_id)
    assert not storage.exists(vectors_key(ready))


def test_spec_019_r17_missing_image_goes_to_dlq(
    running: HealthState, bootstrap: str, storage: S3Storage
) -> None:
    ready = _ready(storage)
    storage.delete_prefix(ready.segments[1].frames[3].key)
    _publish(bootstrap, ready)

    dead = _wait_for(bootstrap, "frame-encoder.dlq", ready.source_id, timeout_s=60)

    assert dead["executionId"] == str(ready.execution_id)
    assert not storage.exists(vectors_key(ready))


def test_spec_019_nfr1_nfr3_slow_message_is_processed_once_without_rebalance(
    bootstrap: str, storage: S3Storage
) -> None:
    # 처리가 max.poll.interval.ms보다 오래 걸려도 그룹에서 빠지지 않아 한 번만 처리한다.
    ready = _ready(storage, segments=1, crops=0)
    slow_encoder = SlowEncoder(image_bytes(ready.segments[0].frames[0].key))
    slow = Prepared(slow_encoder, EncoderConfig(batch_size=16))
    kafka = {"max.poll.interval.ms": str(POLL_INTERVAL_MS), "session.timeout.ms": "6000"}
    runner = _start(bootstrap, storage, slow, group_id=SLOW_GROUP, kafka=kafka)
    next(runner)
    try:
        _publish(bootstrap, ready)

        assert slow_encoder.finished.wait(timeout=60)
        # 그룹에서 빠졌다면 처리 결과를 커밋하지 못한다(libs/messaging의 같은 검사).
        assert _wait_until(lambda: _committed(bootstrap, SLOW_GROUP) == _end(bootstrap), 15)
        assert slow_encoder.started == 1
    finally:
        next(runner, None)


def _committed(bootstrap: str, group_id: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": group_id})
    try:
        [tp] = consumer.committed([TopicPartition("segment.ready", 0)], timeout=10)
        return int(tp.offset)
    finally:
        consumer.close()


def _end(bootstrap: str) -> int:
    consumer = Consumer({"bootstrap.servers": bootstrap, "group.id": f"w-{uuid.uuid4().hex}"})
    try:
        _, high = consumer.get_watermark_offsets(TopicPartition("segment.ready", 0), timeout=10)
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
