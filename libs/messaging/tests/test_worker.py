import asyncio
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from confluent_kafka import Consumer, Message, Producer, TopicPartition

from vlm_contracts.events import VideoDeleted
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.retry import RetryPolicy
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
) -> KafkaWorker:
    config = WorkerConfig(
        bootstrap=setup.bootstrap, group_id=setup.group, retry=retry, kafka=kafka or {}
    )
    return KafkaWorker(config, routes, setup.publisher)


def _committed(setup: Setup) -> int:
    consumer = Consumer({"bootstrap.servers": setup.bootstrap, "group.id": setup.group})
    try:
        [tp] = consumer.committed([TopicPartition(setup.topic, 0)], timeout=10)
        return tp.offset
    finally:
        consumer.close()


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
    assert got == event
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

    dead: list[Message] = []
    assert _wait_until(lambda: bool(dead.extend(read_all(dlq_topic(setup.group), 1.0)) or dead), 30)
    headers = dict(dead[0].headers() or [])
    assert headers["x-original-topic"] == setup.topic.encode()
    assert headers["x-attempt"] == b"3"
    assert b"always broken" in headers["x-error"]
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

    dead: list[Message] = []
    assert _wait_until(lambda: bool(dead.extend(read_all(dlq_topic(setup.group), 1.0)) or dead), 30)
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

    dead: list[Message] = []
    assert _wait_until(lambda: bool(dead.extend(read_all(dlq_topic(setup.group), 1.0)) or dead), 30)
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
        await asyncio.sleep(8)
        done.set()

    setup.publisher.publish(setup.topic, _event())
    start(
        _worker(
            setup,
            [route(setup.topic, VideoDeleted, handle)],
            kafka={"max.poll.interval.ms": "3000", "session.timeout.ms": "6000"},
        )
    )

    assert done.wait(30)
    assert _wait_until(lambda: _committed(setup) == 1, timeout=10)
