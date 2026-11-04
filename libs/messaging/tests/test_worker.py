import asyncio
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from confluent_kafka import Consumer, Message, Producer, TopicPartition
from vlm_common.health import HealthState
from vlm_contracts.events import VideoDeleted
from vlm_contracts.stages import StageTimingBatch
from vlm_messaging.publisher import KafkaPublisher, PublishError
from vlm_messaging.retry import RetryPolicy
from vlm_messaging.timing import KafkaTimingSink, TimingSink
from vlm_messaging.topics import dlq_topic, retry_topic
from vlm_messaging.worker import (
    KafkaWorker,
    MessageContext,
    NonRetryableError,
    Route,
    WorkerConfig,
    route,
)

pytestmark = pytest.mark.integration

FAST_RETRY = RetryPolicy(max_attempts=3, base_delay_s=0.2, max_delay_s=1.0)


@dataclass
class Setup:
    topic: str
    group: str
    bootstrap: str
    publisher: KafkaPublisher


@pytest.fixture
def setup(
    bootstrap: str, unique: Callable[[str], str], create_topics: Callable[..., None]
) -> Setup:
    topic = unique("video.deleted")
    group = unique("worker")
    create_topics(topic, retry_topic(topic, group), dlq_topic(group))
    return Setup(topic, group, bootstrap, KafkaPublisher(bootstrap=bootstrap, client_id="test"))


@pytest.fixture
def start() -> Iterator[Callable[[KafkaWorker], threading.Thread]]:
    workers: list[tuple[KafkaWorker, threading.Thread]] = []

    def run(worker: KafkaWorker) -> threading.Thread:
        thread = threading.Thread(target=worker.run, daemon=True)
        thread.start()
        workers.append((worker, thread))
        return thread

    yield run
    for worker, thread in workers:
        worker.stop()
        thread.join(timeout=15)


def _event() -> VideoDeleted:
    return VideoDeleted(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        source_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
    )


def _worker(
    setup: Setup,
    routes: Sequence[Route],
    retry: RetryPolicy = FAST_RETRY,
    kafka: dict[str, str] | None = None,
    health: HealthState | None = None,
    timing: TimingSink | None = None,
) -> KafkaWorker:
    # 소비 그룹 이름은 테스트마다 다르므로 단계 ID 앞부분은 목록에 있는 서비스 이름을 쓴다.
    config = WorkerConfig(
        bootstrap=setup.bootstrap,
        group_id=setup.group,
        retry=retry,
        kafka=kafka or {},
        service="normalize",
    )
    return KafkaWorker(config, routes, setup.publisher, health=health, timing=timing)


def _committed(setup: Setup) -> int:
    consumer = Consumer({"bootstrap.servers": setup.bootstrap, "group.id": setup.group})
    try:
        [tp] = consumer.committed([TopicPartition(setup.topic, 0)], timeout=10)
        return tp.offset
    finally:
        consumer.close()


def _wait_for_messages(
    read_all: Callable[[str, float], list[Message]], topic: str, timeout: float
) -> list[Message]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        messages = read_all(topic, 1.0)
        if messages:
            return messages
    return []


def _header(msg: Message, name: str) -> bytes:
    headers = msg.headers() or []
    items = headers.items() if isinstance(headers, dict) else headers
    for key, value in items:
        if key == name and isinstance(value, bytes):
            return value
    raise AssertionError(f"header {name} missing")


def _wait_until(condition: Callable[[], bool], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_handler_receives_parsed_event_and_offset_is_committed(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    received: queue.Queue[tuple[VideoDeleted, MessageContext]] = queue.Queue()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        received.put((event, ctx))

    event = _event()
    setup.publisher.publish(setup.topic, event)
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    got, ctx = received.get(timeout=30)
    # 발행기가 봉투에 발행 시각을 채운다(SPEC-013 R-8).
    assert got.produced_at is not None
    assert got.model_copy(update={"produced_at": None}) == event
    assert ctx.topic == setup.topic
    assert ctx.attempt == 1
    assert _wait_until(lambda: _committed(setup) == 1, timeout=10)


def test_failed_message_is_retried_through_retry_topic(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    attempts: queue.Queue[int] = queue.Queue()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        attempts.put(ctx.attempt)
        if ctx.attempt == 1:
            raise RuntimeError("temporary")

    setup.publisher.publish(setup.topic, _event())
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    assert attempts.get(timeout=30) == 1
    assert attempts.get(timeout=30) == 2


def test_retry_waits_for_backoff(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    calls: queue.Queue[float] = queue.Queue()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        calls.put(time.monotonic())
        if ctx.attempt == 1:
            raise RuntimeError("temporary")

    slow = RetryPolicy(max_attempts=3, base_delay_s=2.0, max_delay_s=2.0)
    setup.publisher.publish(setup.topic, _event())
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)], retry=slow))

    first = calls.get(timeout=30)
    second = calls.get(timeout=30)
    # 지터는 지연의 절반 이상을 남긴다.
    assert second - first >= 1.0


def test_exhausted_message_goes_to_dlq_with_reason(
    setup: Setup,
    start: Callable[[KafkaWorker], threading.Thread],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        raise RuntimeError("always broken")

    event = _event()
    setup.publisher.publish(setup.topic, event)
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    dead = _wait_for_messages(read_all, dlq_topic(setup.group), 30)
    assert dead
    assert _header(dead[0], "x-original-topic") == setup.topic.encode()
    assert _header(dead[0], "x-attempt") == b"3"
    assert b"always broken" in _header(dead[0], "x-error")
    assert dead[0].key() == str(event.source_id).encode()


def test_non_retryable_error_goes_straight_to_dlq(
    setup: Setup,
    start: Callable[[KafkaWorker], threading.Thread],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    calls: list[int] = []

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        calls.append(ctx.attempt)
        raise NonRetryableError("unsupported codec")

    setup.publisher.publish(setup.topic, _event())
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    assert _wait_for_messages(read_all, dlq_topic(setup.group), 30)
    assert calls == [1]


def test_invalid_payload_goes_to_dlq_without_calling_handler(
    setup: Setup,
    start: Callable[[KafkaWorker], threading.Thread],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    calls: list[VideoDeleted] = []

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        calls.append(event)

    producer = Producer({"bootstrap.servers": setup.bootstrap})
    producer.produce(setup.topic, key=b"k", value=b'{"eventType": "video.deleted"}')
    producer.flush(10)
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    assert _wait_for_messages(read_all, dlq_topic(setup.group), 30)
    assert calls == []
    assert _wait_until(lambda: _committed(setup) == 1, timeout=10)


def test_stop_during_processing_leaves_offset_uncommitted(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    started = threading.Event()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        started.set()
        await asyncio.sleep(30)

    setup.publisher.publish(setup.topic, _event())
    worker = _worker(setup, [route(setup.topic, VideoDeleted, handle)])
    thread = start(worker)
    assert started.wait(30)

    worker.stop()
    thread.join(timeout=15)

    assert not thread.is_alive()
    # 커밋한 적이 없으면 offset은 음수(OFFSET_INVALID)다.
    assert _committed(setup) < 0


def test_long_handler_keeps_group_membership(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    done = threading.Event()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        # max.poll.interval.ms보다 오래 걸려도 poll을 계속 불러 그룹에서 쫓겨나지 않아야 한다.
        await asyncio.sleep(10)
        done.set()

    setup.publisher.publish(setup.topic, _event())
    start(
        _worker(
            setup,
            [route(setup.topic, VideoDeleted, handle)],
            kafka={"max.poll.interval.ms": "6000", "session.timeout.ms": "6000"},
        )
    )

    assert done.wait(30)
    assert _wait_until(lambda: _committed(setup) == 1, timeout=10)


async def _ignore(event: VideoDeleted, ctx: MessageContext) -> None:
    return None


def test_r23_ready_after_subscribe_and_alive_while_idle(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    """[SPEC-007 R-22, R-23] 구독을 시작하면 준비 상태가 되고, 메시지가 없어도 루프가 살아 있다."""
    health = HealthState(liveness_window_s=2.0)
    assert not health.is_ready()

    start(_worker(setup, [route(setup.topic, VideoDeleted, _ignore)], health=health))

    assert _wait_until(health.is_ready, timeout=10)
    time.sleep(3)
    assert health.is_alive()


class FailingSink:
    def emit(self, batch: StageTimingBatch, *, traceparent: str | None) -> None:
        raise PublishError("sink down")


def test_spec013_r5_worker_logs_one_batch_per_handler(
    setup: Setup,
    start: Callable[[KafkaWorker], threading.Thread],
    caplog: pytest.LogCaptureFixture,
) -> None:
    event = _event()
    setup.publisher.publish(setup.topic, event)
    caplog.set_level(logging.INFO, logger="vlm_messaging.timing")

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        with ctx.timing.stage("normalize.probe"):
            pass

    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)]))

    assert _wait_until(lambda: _committed(setup) == 1, timeout=30)
    [record] = [r for r in caplog.records if r.getMessage() == "stage timing"]
    batch = StageTimingBatch.model_validate(record.__dict__["stageTiming"])
    assert batch.service == "normalize"
    # 발행기가 producedAt을 채웠으므로 큐 대기도 남는다(R-1, R-8).
    assert [t.path for t in batch.timings] == [
        "normalize.queue_wait",
        "normalize.handle/normalize.probe",
        "normalize.handle",
    ]
    assert {t.subject.source_id for t in batch.timings} == {event.source_id}


def test_spec013_r5_failed_handler_still_sends_batch_to_kafka(
    setup: Setup,
    start: Callable[[KafkaWorker], threading.Thread],
    unique: Callable[[str], str],
    create_topics: Callable[..., None],
    read_all: Callable[[str, float], list[Message]],
) -> None:
    timing_topic = unique("stage.timing")
    create_topics(timing_topic)
    event = _event()
    setup.publisher.publish(setup.topic, event)

    async def fail(event: VideoDeleted, ctx: MessageContext) -> None:
        raise NonRetryableError("bad video")

    sink = KafkaTimingSink(setup.publisher, topic=timing_topic)
    start(_worker(setup, [route(setup.topic, VideoDeleted, fail)], timing=sink))

    [msg] = _wait_for_messages(read_all, timing_topic, timeout=30)
    assert msg.key() == str(event.source_id).encode()
    batch = StageTimingBatch.model_validate_json(msg.value() or b"")
    handle = batch.timings[-1]
    assert (handle.stage_id, handle.status) == ("normalize.handle", "error")


def test_spec013_r6_sink_failure_does_not_change_commit(
    setup: Setup, start: Callable[[KafkaWorker], threading.Thread]
) -> None:
    calls: queue.Queue[VideoDeleted] = queue.Queue()

    async def handle(event: VideoDeleted, ctx: MessageContext) -> None:
        calls.put(event)

    setup.publisher.publish(setup.topic, _event())
    start(_worker(setup, [route(setup.topic, VideoDeleted, handle)], timing=FailingSink()))

    assert _wait_until(lambda: _committed(setup) == 1, timeout=30)
    calls.get(timeout=1)
    # 측정 실패로 메시지를 다시 처리하지 않는다.
    assert calls.empty()
