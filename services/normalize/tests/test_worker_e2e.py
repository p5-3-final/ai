"""실제 Kafka·SeaweedFS·ffmpeg로 normalize 워커 전체를 검증한다(Docker 안에서 실행)."""

import json
import subprocess
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from confluent_kafka import Consumer
from normalize.__main__ import build_worker
from normalize.ids import hls_playlist_key, normalized_key
from normalize.settings import NormalizeSettings, WorkerSettings
from vlm_common.health import HealthState
from vlm_contracts.events import VideoUploaded
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = [pytest.mark.ffmpeg, pytest.mark.integration]

BUCKET = "vlm-media"
TOPICS = [
    "video.uploaded",
    "video.uploaded.normalize.retry",
    "normalize.dlq",
    "video.normalizing",
    "video.normalized",
]


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
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(scope="module")
def running(
    bootstrap: str, storage: S3Storage, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[HealthState]:
    health = HealthState()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        NormalizeSettings(work_dir=tmp_path_factory.mktemp("work")),
        storage,
        health,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


def _upload(storage: S3Storage, bootstrap: str, path: Path) -> uuid.UUID:
    source = uuid.uuid4()
    key = f"sources/{source}/original"
    storage.put_file(key, path, "video/mp4")
    event = VideoUploaded(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        source_id=source,
        owner_id=uuid.uuid4(),
        storage_key=key,
        size_bytes=path.stat().st_size,
        content_type="video/mp4",
    )
    KafkaPublisher(bootstrap=bootstrap, client_id="test").publish("video.uploaded", event)
    return source


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
        for _ in range(int(timeout_s / 0.5)):
            msg = consumer.poll(0.5)
            if msg is None or msg.error() is not None:
                continue
            payload: dict[str, object] = json.loads(msg.value() or b"{}")
            if payload["sourceId"] == str(source):
                return payload
    finally:
        consumer.close()
    raise AssertionError(f"{topic} for {source} not received")


def _video(tmp_path: Path) -> Path:
    path = tmp_path / "in.mp4"
    subprocess.run(  # noqa: S603  # 테스트 입력 생성. 인자는 고정값이다 (#8)
        [  # noqa: S607  # 이미지의 ffmpeg (#8)
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=25",
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def test_r23_worker_becomes_ready(running: HealthState) -> None:
    for _ in range(40):
        if running.is_ready():
            return
        threading.Event().wait(0.25)
    raise AssertionError("worker not ready")


def test_uploaded_video_becomes_ready_with_stored_outputs(
    running: HealthState, bootstrap: str, storage: S3Storage, tmp_path: Path
) -> None:
    source = _upload(storage, bootstrap, _video(tmp_path))

    started = _wait_for(bootstrap, "video.normalizing", source, timeout_s=30)
    result = _wait_for(bootstrap, "video.normalized", source, timeout_s=60)

    assert started["eventType"] == "video.normalizing"
    assert result["result"] == "READY"
    assert result["fps"] == 25
    assert result["transcoded"] is False
    assert storage.exists(normalized_key(source))
    assert storage.exists(hls_playlist_key(source))


def test_non_video_file_is_rejected(
    running: HealthState, bootstrap: str, storage: S3Storage, tmp_path: Path
) -> None:
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00junk" * 100)
    source = _upload(storage, bootstrap, junk)

    result = _wait_for(bootstrap, "video.normalized", source, timeout_s=60)

    assert (result["result"], result["rejectReason"]) == ("REJECTED", "UNREADABLE")
    assert not storage.exists(normalized_key(source))
