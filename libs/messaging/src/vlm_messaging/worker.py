"""Kafka 소비 워커.

메시지를 하나씩 처리하고, 처리 결과가 정해진 뒤에만 offset을 커밋한다.

- 성공: 커밋한다.
- 실패: 재시도 토픽(`{topic}.{group}.retry`)으로 옮기고 커밋한다. 재시도 메시지는 지연 시각이
  될 때까지 그 파티션을 멈춰 둔다.
- 재시도 소진, `NonRetryableError`, 계약과 맞지 않는 메시지: DLQ(`{group}.dlq`)로 옮기고 커밋한다.
- 처리 중 종료 신호를 받거나 파티션을 잃으면 커밋하지 않는다. 다시 받은 메시지는 결정적 ID로
  흡수한다(docs overview "여러 Pod 운영 원칙").
"""

import asyncio
import logging
import random
import signal
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, field

from confluent_kafka import Consumer, KafkaException, Message, TopicPartition
from pydantic import ValidationError
from vlm_common.errors import VlmError
from vlm_contracts.events import Event

from vlm_messaging.publisher import Headers, KafkaPublisher
from vlm_messaging.retry import RetryPolicy
from vlm_messaging.topics import dlq_topic, retry_topic

ATTEMPT_HEADER = "x-attempt"
NOT_BEFORE_HEADER = "x-not-before-ms"
ORIGINAL_TOPIC_HEADER = "x-original-topic"
ERROR_HEADER = "x-error"
TRACEPARENT_HEADER = "traceparent"
# 헤더가 커지면 브로커의 메시지 크기 제한에 걸리므로 오류 설명은 앞부분만 남긴다.
_ERROR_HEADER_LIMIT = 1024

logger = logging.getLogger(__name__)


class NonRetryableError(VlmError):
    """다시 시도해도 결과가 같은 실패. 재시도 없이 DLQ로 보낸다(예: 지원하지 않는 코덱)."""

    code = "MESSAGING_NON_RETRYABLE"


@dataclass(frozen=True, slots=True)
class MessageContext:
    """핸들러에 이벤트와 함께 넘기는 메시지 정보.

    Attributes:
        topic: 원래 토픽. 재시도 토픽에서 받은 메시지도 원래 토픽 이름이다.
        partition: 받은 파티션.
        offset: 받은 offset.
        key: 메시지 키.
        attempt: 시도 번호(첫 시도는 1).
        traceparent: W3C 추적 헤더. 결과 이벤트를 발행할 때 그대로 넘긴다.
    """

    topic: str
    partition: int
    offset: int
    key: bytes | None
    attempt: int
    traceparent: str | None


type Dispatch = Callable[[bytes, MessageContext], Coroutine[None, None, None]]


@dataclass(frozen=True, slots=True)
class Route:
    """토픽 하나와 그 메시지를 처리할 함수. `route()`로 만든다."""

    topic: str
    dispatch: Dispatch


def route[E: Event](
    topic: str, model: type[E], handler: Callable[[E, MessageContext], Awaitable[None]]
) -> Route:
    """`topic`의 메시지를 `model`로 검증해 `handler`에 넘기는 경로를 만든다.

    Args:
        topic: 구독할 토픽.
        model: 메시지 본문을 검증할 이벤트 모델.
        handler: 처리 함수. 예외를 던지면 재시도하고, `NonRetryableError`면 DLQ로 보낸다.
            CPU를 오래 쓰는 처리는 `asyncio.to_thread`로 넘긴다.
    """

    async def dispatch(value: bytes, ctx: MessageContext) -> None:
        await handler(model.model_validate_json(value), ctx)

    return Route(topic, dispatch)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """워커 설정.

    Attributes:
        bootstrap: Kafka 부트스트랩 주소.
        group_id: 소비 그룹. 재시도·DLQ 토픽 이름에도 쓴다.
        retry: 재시도 정책.
        kafka: librdkafka 설정 덮어쓰기(예: `max.poll.interval.ms`).
        poll_timeout_s: poll 한 번의 대기 시간.
    """

    bootstrap: str
    group_id: str
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    kafka: Mapping[str, str] = field(default_factory=dict)
    poll_timeout_s: float = 0.5


class KafkaWorker:
    """`Route` 목록을 구독해 메시지를 하나씩 처리하는 워커.

    `run()`은 `stop()`이 불릴 때까지 돌아오지 않는다. 메인 스레드에서 부르면 SIGTERM·SIGINT에
    `stop()`을 연결한다. 재시도·DLQ 토픽은 미리 만들어 두어야 한다(`required_topics()`).
    """

    def __init__(
        self,
        config: WorkerConfig,
        routes: Sequence[Route],
        publisher: KafkaPublisher,
        *,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._routes = {r.topic: r for r in routes}
        self._original_of = {retry_topic(t, config.group_id): t for t in self._routes}
        self._publisher = publisher
        self._rand = rand
        self._clock = clock
        self._stopping = threading.Event()
        # 지연 중인 재시도 파티션 → 다시 읽기 시작할 시각(epoch 초)
        self._delayed: dict[tuple[str, int], float] = {}
        # 처리 중에 빼앗긴 파티션. 그 파티션의 결과는 커밋하지 않는다.
        self._revoked: set[tuple[str, int]] = set()
        self._consumer = Consumer(
            {
                "bootstrap.servers": config.bootstrap,
                "group.id": config.group_id,
                "enable.auto.commit": False,
                "auto.offset.reset": "earliest",
                # 재배치 때 모든 파티션을 내려놓지 않아 긴 처리 중인 파티션을 지킬 수 있다.
                "partition.assignment.strategy": "cooperative-sticky",
                **config.kafka,
            }
        )

    def required_topics(self) -> list[str]:
        """구독 대상과 발행 대상을 합친 토픽 목록. 운영에서 미리 만들어 둘 토픽이다."""
        return [*self._routes, *self._original_of, dlq_topic(self._config.group_id)]

    def stop(self) -> None:
        """처리 중인 메시지를 커밋하지 않고 루프를 끝내게 한다. 어느 스레드에서 불러도 된다."""
        self._stopping.set()

    def run(self) -> None:
        """루프를 실행한다. `stop()`이 불리면 소비 그룹에서 나가고 돌아온다."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda _signum, _frame: self.stop())
        loop = asyncio.new_event_loop()
        # 핸들러는 이 스레드의 이벤트 루프에서 돈다. 소비 스레드는 그동안 poll을 계속 부른다.
        handler_thread = threading.Thread(target=loop.run_forever, daemon=True)
        handler_thread.start()
        self._consumer.subscribe(
            [*self._routes, *self._original_of],
            on_assign=self._on_assign,
            on_revoke=self._on_revoke,
            on_lost=self._on_revoke,
        )
        try:
            while not self._stopping.is_set():
                self._resume_due()
                msg = self._consumer.poll(self._config.poll_timeout_s)
                if msg is None:
                    continue
                error = msg.error()
                if error is not None:
                    if error.fatal():
                        raise KafkaException(error)
                    logger.warning("consumer error", extra={"error": str(error)})
                    continue
                self._handle(msg, loop)
        finally:
            # 자동 커밋을 끈 상태라 close는 offset을 커밋하지 않는다.
            self._consumer.close()
            loop.call_soon_threadsafe(loop.stop)

    def _on_assign(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        logger.info("partitions assigned", extra={"partitions": _names(partitions)})

    def _on_revoke(self, _consumer: Consumer, partitions: list[TopicPartition]) -> None:
        for tp in partitions:
            self._revoked.add((tp.topic, tp.partition))
            self._delayed.pop((tp.topic, tp.partition), None)
        logger.info("partitions revoked", extra={"partitions": _names(partitions)})

    def _resume_due(self) -> None:
        now = self._clock()
        due = [tp for tp, at in self._delayed.items() if at <= now]
        for topic, partition in due:
            del self._delayed[(topic, partition)]
            self._consumer.resume([TopicPartition(topic, partition)])

    def _handle(self, msg: Message, loop: asyncio.AbstractEventLoop) -> None:
        topic, partition, offset = _position(msg)
        original = self._original_of.get(topic, topic)
        not_before = int(_header(msg, NOT_BEFORE_HEADER) or b"0") / 1000
        if self._clock() < not_before:
            # 재시도 시각 전이면 이 메시지부터 다시 읽도록 되감고 파티션을 멈춰 둔다.
            tp = TopicPartition(topic, partition, offset)
            self._consumer.pause([tp])
            self._consumer.seek(tp)
            self._delayed[(topic, partition)] = not_before
            return

        traceparent = _header(msg, TRACEPARENT_HEADER)
        ctx = MessageContext(
            topic=original,
            partition=partition,
            offset=offset,
            key=msg.key(),
            attempt=int(_header(msg, ATTEMPT_HEADER) or b"1"),
            traceparent=traceparent.decode() if traceparent else None,
        )
        future: Future[None] = asyncio.run_coroutine_threadsafe(
            self._routes[original].dispatch(msg.value() or b"", ctx), loop
        )
        if not self._wait(future, (topic, partition)):
            logger.info("message abandoned without commit", extra=_log_fields(ctx))
            return

        failure = future.exception()
        if failure is not None:
            # 계약과 맞지 않는 메시지와 재시도해도 같은 실패는 기다리지 않고 DLQ로 보낸다.
            permanent = isinstance(failure, ValidationError | NonRetryableError)
            if permanent or self._config.retry.exhausted(ctx.attempt):
                self._dead_letter(msg, ctx, failure)
            else:
                self._retry(msg, ctx, failure)
        self._commit(msg, ctx)

    def _wait(self, future: Future[None], source: tuple[str, int]) -> bool:
        """핸들러가 끝날 때까지 poll을 계속 불러 그룹 멤버십을 유지한다.

        `max.poll.interval.ms`보다 오래 걸리는 처리도 그룹에서 쫓겨나지 않게, 처리 중에는 모든
        파티션을 멈추고 poll만 부른다. 결과를 커밋해도 되면 True를 돌려준다.
        """
        self._revoked.clear()
        self._consumer.pause(self._consumer.assignment())
        try:
            while not future.done():
                if self._stopping.is_set():
                    return False
                stray = self._consumer.poll(self._config.poll_timeout_s)
                if stray is not None and stray.error() is None:
                    # 처리 중에 새로 배정된 파티션은 멈춰 있지 않으므로 받은 메시지를 되감아 둔다.
                    topic, partition, offset = _position(stray)
                    tp = TopicPartition(topic, partition, offset)
                    self._consumer.seek(tp)
                    self._consumer.pause([tp])
            return source not in self._revoked
        finally:
            resumable = [
                tp
                for tp in self._consumer.assignment()
                if (tp.topic, tp.partition) not in self._delayed
            ]
            self._consumer.resume(resumable)

    def _retry(self, msg: Message, ctx: MessageContext, failure: BaseException) -> None:
        next_attempt = ctx.attempt + 1
        delay = self._config.retry.delay_before(next_attempt, self._rand())
        not_before_ms = int((self._clock() + delay) * 1000)
        logger.warning(
            "handler failed, scheduling retry",
            extra={**_log_fields(ctx), "error": _describe(failure), "delay_s": delay},
        )
        headers: list[tuple[str, bytes]] = [
            (ATTEMPT_HEADER, str(next_attempt).encode()),
            (NOT_BEFORE_HEADER, str(not_before_ms).encode()),
            (ORIGINAL_TOPIC_HEADER, ctx.topic.encode()),
        ]
        self._publisher.send_raw(
            retry_topic(ctx.topic, self._config.group_id),
            msg.key(),
            msg.value() or b"",
            _with_trace(headers, ctx),
        )

    def _dead_letter(self, msg: Message, ctx: MessageContext, failure: BaseException) -> None:
        logger.error("message sent to DLQ", extra={**_log_fields(ctx), "error": _describe(failure)})
        headers: list[tuple[str, bytes]] = [
            (ORIGINAL_TOPIC_HEADER, ctx.topic.encode()),
            (ATTEMPT_HEADER, str(ctx.attempt).encode()),
            (ERROR_HEADER, _describe(failure).encode()[:_ERROR_HEADER_LIMIT]),
        ]
        dlq = dlq_topic(self._config.group_id)
        self._publisher.send_raw(dlq, msg.key(), msg.value() or b"", _with_trace(headers, ctx))

    def _commit(self, msg: Message, ctx: MessageContext) -> None:
        try:
            self._consumer.commit(message=msg, asynchronous=False)
        except KafkaException as e:
            # 커밋 직전에 파티션을 잃은 경우다. 새 주인이 다시 처리하고 결정적 ID로 흡수한다.
            logger.warning("commit failed", extra={**_log_fields(ctx), "error": str(e)})


def _position(msg: Message) -> tuple[str, int, int]:
    topic, partition, offset = msg.topic(), msg.partition(), msg.offset()
    if topic is None or partition is None or offset is None:
        raise ValueError("consumed message without topic position")
    return topic, partition, offset


def _header(msg: Message, name: str) -> bytes | None:
    headers = msg.headers() or []
    # 소비한 메시지는 목록이지만 타입 정의가 dict도 허용한다.
    items = headers.items() if isinstance(headers, dict) else headers
    for key, value in items:
        if key == name:
            return value.encode() if isinstance(value, str) else value
    return None


def _with_trace(headers: list[tuple[str, bytes]], ctx: MessageContext) -> Headers:
    if ctx.traceparent:
        return [*headers, (TRACEPARENT_HEADER, ctx.traceparent.encode())]
    return headers


def _describe(failure: BaseException) -> str:
    return f"{type(failure).__name__}: {failure}"


def _names(partitions: list[TopicPartition]) -> list[str]:
    return [f"{tp.topic}[{tp.partition}]" for tp in partitions]


def _log_fields(ctx: MessageContext) -> dict[str, object]:
    return {
        "topic": ctx.topic,
        "partition": ctx.partition,
        "offset": ctx.offset,
        "attempt": ctx.attempt,
    }
