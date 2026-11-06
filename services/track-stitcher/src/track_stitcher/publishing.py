"""이벤트 발행기 Protocol. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

from typing import Protocol

from vlm_contracts.events import Event


class EventPublisher(Protocol):
    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...
