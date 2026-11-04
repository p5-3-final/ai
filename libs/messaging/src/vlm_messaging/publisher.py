"""이벤트 발행."""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from confluent_kafka import KafkaError, KafkaException, Message, Producer
from vlm_common.errors import VlmError
from vlm_contracts.events import Event

Headers = Sequence[tuple[str, bytes]]


class PublishError(VlmError):
    """브로커가 메시지를 받았다고 확인하지 못했다."""

    code = "MESSAGING_PUBLISH_FAILED"


class KafkaPublisher:
    """이벤트를 Kafka에 발행하고 브로커 확인(acks=all)까지 기다린다.

    워커는 결과를 발행한 뒤에야 입력 offset을 커밋하므로, 발행이 확인되기 전에는 돌아오지 않는다.
    """

    def __init__(
        self,
        bootstrap: str,
        client_id: str,
        timeout_s: float = 10.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._timeout_s = timeout_s
        self._now = now
        self._producer = Producer(
            {
                "bootstrap.servers": bootstrap,
                "client.id": client_id,
                "acks": "all",
                # 재전송으로 같은 메시지가 두 번 쌓이지 않게 한다.
                "enable.idempotence": True,
                "message.timeout.ms": int(timeout_s * 1000),
            }
        )

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        """이벤트를 camelCase JSON으로 발행한다. 봉투의 `producedAt`은 발행 시각으로 채운다.

        Args:
            topic: 토픽 이름.
            event: 발행할 이벤트.
            key: 메시지 키. 없으면 envelope의 sourceId다(청크 단위 토픽은 chunkId를 넘긴다).
            traceparent: W3C 추적 헤더. 있으면 그대로 싣는다.

        Raises:
            PublishError: 제한 시간 안에 브로커 확인을 받지 못했을 때.
        """
        headers = [("traceparent", traceparent.encode())] if traceparent else []
        # 소비자가 큐 대기 시간을 재도록 보내기 직전 시각을 싣는다(SPEC-013 R-8).
        stamped = event.model_copy(update={"produced_at": self._now()})
        value = stamped.model_dump_json(by_alias=True).encode()
        self.send_raw(topic, key or str(event.source_id), value, headers)

    def send_raw(self, topic: str, key: str | bytes | None, value: bytes, headers: Headers) -> None:
        """받은 메시지를 그대로 다른 토픽(retry, DLQ)으로 옮길 때 쓴다."""
        errors: list[KafkaError] = []

        def on_delivery(err: KafkaError | None, _msg: Message) -> None:
            if err is not None:
                errors.append(err)

        try:
            self._producer.produce(
                topic, key=key, value=value, headers=list(headers), on_delivery=on_delivery
            )
        except (KafkaException, BufferError) as e:
            raise PublishError(f"failed to enqueue message for {topic}: {e}") from e
        # 전송 제한 시간이 먼저 끝나 delivery 오류로 알려지도록 flush는 조금 더 기다린다.
        if self._producer.flush(self._timeout_s + 1.0) > 0:
            raise PublishError(f"delivery to {topic} not confirmed within {self._timeout_s}s")
        if errors:
            raise PublishError(f"delivery to {topic} failed: {errors[0]}")
