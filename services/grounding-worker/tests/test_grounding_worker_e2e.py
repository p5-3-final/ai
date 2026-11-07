"""실제 Kafka·SeaweedFS·ffmpeg로 grounding-worker 워커 전체를 검증한다. 탐지기는 mock이다.

서비스 이미지 안에서 돌린다(`scripts/test-in-docker.sh grounding-worker`). ffmpeg가 서명 주소로
SeaweedFS의 정규화 사본을 직접 읽는다.
"""

import asyncio
import json
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from confluent_kafka import Consumer, TopicPartition
from grounding_worker.__main__ import build_worker, prepare
from grounding_worker.media import FfmpegTool, MediaInfo
from grounding_worker.settings import GroundingWorkerSettings, WorkerSettings
from gw_support import (
    NORMALIZATION,
    SOURCE,
    event_errors,
    grounding_request,
    result_errors,
    segment,
)
from vlm_common.health import HealthState
from vlm_contracts.events import GroundingCompleted
from vlm_messaging.publisher import KafkaPublisher
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = [pytest.mark.integration, pytest.mark.ffmpeg]

TOPIC = "grounding.requested"
GROUP = "grounding-worker"
SLOW_GROUP = "grounding-worker-slow"
BUCKET = "grounding-e2e"
POLL_INTERVAL_MS = 6000
_MOCK = Path(__file__).resolve().parents[1] / "profiles" / "local-mock.toml"


def _topics(group: str) -> list[str]:
    return [f"{TOPIC}.{group}.retry", f"{group}.dlq"]


@pytest.fixture(scope="module")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        containers.create_topics(
            address, [TOPIC, "grounding.completed", *_topics(GROUP), *_topics(SLOW_GROUP)]
        )
        yield address


@pytest.fixture(scope="module")
def monkeypatch_module() -> Iterator[pytest.MonkeyPatch]:
    with pytest.MonkeyPatch.context() as mp:
        yield mp


@pytest.fixture(scope="module")
def storage(monkeypatch_module: pytest.MonkeyPatch) -> Iterator[S3Storage]:
    with containers.seaweedfs() as endpoint:
        containers.create_bucket(endpoint, BUCKET)
        monkeypatch_module.setenv("AWS_ACCESS_KEY_ID", containers.TEST_ACCESS_KEY)
        monkeypatch_module.setenv("AWS_SECRET_ACCESS_KEY", containers.TEST_ACCESS_KEY)
        yield S3Storage(StorageSettings(bucket=BUCKET, endpoint_url=endpoint, path_style=True))


@pytest.fixture(scope="module")
def video(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("src") / "normalized.mp4"
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    args += ["-f", "lavfi", "-i", "testsrc=s=320x240:r=30:d=6"]
    args += ["-f", "lavfi", "-i", "sine=f=440:d=6", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path)]
    subprocess.run(args, check=True)  # noqa: S603  # 테스트가 만든 인자 목록이다 (#48)
    return path.read_bytes()


class _SlowMedia(FfmpegTool):
    """probe 전에 그룹 하트비트 기준보다 오래 기다린다(R-16 확인용)."""

    def __init__(self) -> None:
        super().__init__()
        self.finished = threading.Event()

    async def probe(self, url: str) -> MediaInfo:
        if not self.finished.is_set() and url.startswith("http"):
            await asyncio.sleep(POLL_INTERVAL_MS / 1000 + 2)
            self.finished.set()
        return await super().probe(url)


def _start(
    bootstrap: str,
    storage: S3Storage,
    work: Path,
    *,
    group_id: str = GROUP,
    kafka: dict[str, str] | None = None,
    media: FfmpegTool | None = None,
) -> Iterator[HealthState]:
    health = HealthState()
    settings = GroundingWorkerSettings(
        profile_path=_MOCK, allow_mock=True, group_id=group_id, work_dir=work
    )
    worker = build_worker(
        WorkerSettings(kafka_bootstrap=bootstrap),
        settings,
        storage,
        prepare(settings),
        health,
        kafka=kafka,
        media=media,
    )
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    yield health
    worker.stop()
    thread.join(timeout=15)


@pytest.fixture(scope="module")
def running(
    bootstrap: str, storage: S3Storage, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[HealthState]:
    yield from _start(bootstrap, storage, tmp_path_factory.mktemp("work"))


def _body(storage: S3Storage, video: bytes, end_ms: int = 3_000) -> dict[str, object]:
    """테스트끼리 결과를 나누려고 영상마다 다른 sourceId를 쓰고 그 폴더에 원본을 올린다."""
    source = uuid.uuid4()
    message = grounding_request([segment(0, 1_000, end_ms)])
    text = message.model_dump_json(by_alias=True).replace(str(SOURCE), str(source))
    body: dict[str, object] = json.loads(text)
    segments = body["segments"]
    assert isinstance(segments, list)
    segments[0] |= {"groundingId": str(uuid.uuid4()), "segmentId": str(uuid.uuid4())}
    storage.put_bytes(str(body["normalizedKey"]), video, "video/mp4")
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


def test_spec026_us1_request_becomes_ready_clip_and_result(
    running: HealthState, bootstrap: str, storage: S3Storage, video: bytes
) -> None:
    body = _body(storage, video)
    _send(bootstrap, body)

    [published] = _collect(bootstrap, "grounding.completed", body["sourceId"], 1, timeout_s=90)

    event = GroundingCompleted.model_validate(published)
    assert event_errors(event) == []
    assert event.outcome == "READY"
    assert event.frames is not None
    assert [f.pts_ms for f in event.frames] == [1_000, 2_000]
    assert event.clip_key is not None
    assert event.result_key is not None
    assert storage.get_bytes(event.clip_key)[4:8] == b"ftyp"
    assert result_errors(storage.get_bytes(event.result_key)) == []


def test_spec026_us2_same_request_again_republishes_the_same_event(
    running: HealthState, bootstrap: str, storage: S3Storage, video: bytes
) -> None:
    body = _body(storage, video)
    _send(bootstrap, body)
    [first] = _collect(bootstrap, "grounding.completed", body["sourceId"], 1, timeout_s=90)
    clip = storage.get_bytes(str(first["clipKey"]))

    _send(bootstrap, {**body, "eventId": str(uuid.uuid4())})

    events = _collect(bootstrap, "grounding.completed", body["sourceId"], 2, timeout_s=90)
    assert events[1]["eventId"] == first["eventId"]
    assert events[1]["frames"] == first["frames"]
    assert storage.get_bytes(str(first["clipKey"])) == clip


def test_spec026_us3_segment_past_the_end_is_failed(
    running: HealthState, bootstrap: str, storage: S3Storage, video: bytes
) -> None:
    body = _body(storage, video, end_ms=60_000)
    _send(bootstrap, body)

    [published] = _collect(bootstrap, "grounding.completed", body["sourceId"], 1, timeout_s=90)

    assert (published["outcome"], published["errorCode"]) == (
        "FAILED",
        "GROUNDING_SEGMENT_OUT_OF_RANGE",
    )


@pytest.mark.parametrize("change", ["normalizedKey", "schema"])
def test_spec026_r15_invalid_message_goes_to_dlq(
    running: HealthState, bootstrap: str, storage: S3Storage, video: bytes, change: str
) -> None:
    body = _body(storage, video)
    if change == "normalizedKey":
        other = str(body["normalizedKey"]).replace(str(NORMALIZATION), str(uuid.uuid4()))
        body["normalizedKey"] = other
    else:
        body["targets"] = []
    _send(bootstrap, body)

    [dead] = _collect(bootstrap, f"{GROUP}.dlq", body["sourceId"], 1, timeout_s=60)

    assert dead["eventId"] == body["eventId"]
    _collect(bootstrap, "grounding.completed", body["sourceId"], 0, timeout_s=3)


def test_spec026_r16_nfr3_slow_processing_is_committed_once_without_rebalance(
    bootstrap: str, storage: S3Storage, video: bytes, tmp_path: Path
) -> None:
    """[SPEC-026 R-16, NFR-3] max.poll.interval.ms보다 오래 걸려도 그룹에 남아 한 번 커밋한다."""
    body = _body(storage, video)
    media = _SlowMedia()
    kafka = {"max.poll.interval.ms": str(POLL_INTERVAL_MS), "session.timeout.ms": "6000"}
    runner = _start(bootstrap, storage, tmp_path, group_id=SLOW_GROUP, kafka=kafka, media=media)
    next(runner)
    try:
        _send(bootstrap, body)

        assert media.finished.wait(timeout=60)
        _collect(bootstrap, "grounding.completed", body["sourceId"], 1, timeout_s=60)
        assert _wait_until(lambda: _committed(bootstrap, SLOW_GROUP) == _end(bootstrap), 15)
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
