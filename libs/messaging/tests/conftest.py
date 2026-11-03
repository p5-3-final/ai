import socket
import uuid
from collections.abc import Callable, Iterator

import pytest
from confluent_kafka import Consumer, Message
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

# 로컬 compose(local/docker-compose.yml)와 같은 이미지로 검증한다.
KAFKA_IMAGE = (
    "apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture(scope="session")
def bootstrap() -> Iterator[str]:
    # 광고 주소에 호스트 포트가 들어가야 해서 포트를 먼저 정하고 같은 번호로 연결한다.
    port = _free_port()
    container = (
        DockerContainer(KAFKA_IMAGE)
        .with_bind_ports(9092, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", "HOST://:9092,CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"HOST://127.0.0.1:{port}")
        .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "HOST:PLAINTEXT,CONTROLLER:PLAINTEXT")
        .with_env("KAFKA_INTER_BROKER_LISTENER_NAME", "HOST")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
        .waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(60))
    )
    with container:
        yield f"127.0.0.1:{port}"


@pytest.fixture
def create_topics(bootstrap: str) -> Callable[..., None]:
    def create(*names: str) -> None:
        admin = AdminClient({"bootstrap.servers": bootstrap})
        futures = admin.create_topics([NewTopic(n, num_partitions=1) for n in names])
        for future in futures.values():
            future.result(timeout=30)

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
