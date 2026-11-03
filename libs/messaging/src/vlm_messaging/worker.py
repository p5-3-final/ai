from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field

from vlm_contracts.events import Event
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.retry import RetryPolicy


class NonRetryableError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class MessageContext:
    topic: str
    attempt: int


@dataclass(frozen=True, slots=True)
class Route:
    topic: str


def route[E: Event](
    topic: str, model: type[E], handler: Callable[[E, MessageContext], Awaitable[None]]
) -> Route:
    return Route(topic)


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    bootstrap: str
    group_id: str
    retry: RetryPolicy = RetryPolicy()
    kafka: Mapping[str, str] = field(default_factory=dict)


class KafkaWorker:
    def __init__(
        self, config: WorkerConfig, routes: Sequence[Route], publisher: KafkaPublisher
    ) -> None:
        pass

    def run(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        pass
