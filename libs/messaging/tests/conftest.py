import uuid
from collections.abc import Callable, Iterator

import pytest
from confluent_kafka import Consumer, Message
from vlm_testing import containers


@pytest.fixture(scope="session")
def bootstrap() -> Iterator[str]:
    with containers.kafka() as address:
        yield address


@pytest.fixture
def create_topics(bootstrap: str) -> Callable[..., None]:
    def create(*names: str) -> None:
        containers.create_topics(bootstrap, names)

    return create


@pytest.fixture
def unique() -> Callable[[str], str]:
    suffix = uuid.uuid4().hex[:8]
    return lambda name: f"{name}-{suffix}"


@pytest.fixture
def read_all(bootstrap: str) -> Callable[[str, float], list[Message]]:
    """토픽을 처음부터 읽어 `timeout` 동안 받은 메시지를 돌려준다."""

    def read(topic: str, timeout: float) -> list[Message]:
        consumer = Consumer(
            {
                "bootstrap.servers": bootstrap,
                "group.id": f"reader-{uuid.uuid4().hex}",
                "auto.offset.reset": "earliest",
                "enable.auto.commit": False,
            }
        )
        consumer.subscribe([topic])
        messages: list[Message] = []
        try:
            for _ in range(int(timeout / 0.5)):
                msg = consumer.poll(0.5)
                if msg is not None and msg.error() is None:
                    messages.append(msg)
        finally:
            consumer.close()
        return messages

    return read
