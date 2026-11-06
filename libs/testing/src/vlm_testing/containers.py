"""통합 테스트용 Kafka·SeaweedFS·PostgreSQL·Redis 컨테이너.

로컬 compose(docs `local/`)와 같은 이미지를 쓴다.
"""

import socket
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from confluent_kafka.admin import AdminClient
from confluent_kafka.cimpl import NewTopic
from pydantic_settings import BaseSettings
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

KAFKA_IMAGE = (
    "apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7"
)
SEAWEEDFS_IMAGE = (
    "chrislusf/seaweedfs:4.48"
    "@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"
)
POSTGRES_IMAGE = (
    "postgres:17.11-alpine@sha256:266d3c257594e195b309ae9d1805e54c788f1d7b375f56bf6c367aac6c917e7b"
)
REDIS_IMAGE = (
    "redis:7.4.12-alpine@sha256:8da19c7399dca900c6822a1cf30f9d2285585e2c4b457ff4095b0f4500054d3f"
)
# 접근 키 설정 없이 띄운 SeaweedFS는 서명을 검사하지 않는다. 테스트 자격 증명은 아무 값이나 된다.
TEST_ACCESS_KEY = "test"


class _DockerHostSettings(BaseSettings):
    # 테스트를 컨테이너 안에서 돌리면(scripts/test-in-docker.sh)
    # 형제 컨테이너는 호스트를 거쳐 닿는다.
    testcontainers_host_override: str = "127.0.0.1"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@contextmanager
def kafka() -> Iterator[str]:
    """단일 노드 Kafka를 띄우고 부트스트랩 주소를 돌려준다."""
    host = _DockerHostSettings().testcontainers_host_override
    # 광고 주소에 호스트 포트가 들어가야 해서 포트를 먼저 정하고 같은 번호로 연결한다.
    port = _free_port()
    container = (
        DockerContainer(KAFKA_IMAGE)
        .with_bind_ports(9092, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", "HOST://:9092,CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"HOST://{host}:{port}")
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
        yield f"{host}:{port}"


def create_topics(bootstrap: str, names: Sequence[str]) -> None:
    """파티션 하나짜리 토픽을 만든다."""
    admin = AdminClient({"bootstrap.servers": bootstrap})
    futures = admin.create_topics([NewTopic(n, num_partitions=1) for n in names])
    for future in futures.values():
        future.result(timeout=30)


@contextmanager
def seaweedfs() -> Iterator[str]:
    """S3 게이트웨이를 켠 SeaweedFS를 띄우고 S3 주소를 돌려준다."""
    container = DockerContainer(SEAWEEDFS_IMAGE).with_command("server -s3 -dir=/data")
    container.with_exposed_ports(8333)
    with container:
        yield f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8333)}"


def create_bucket(endpoint: str, bucket: str, timeout_s: float = 60.0) -> None:
    """버킷을 만들고 객체를 쓸 수 있을 때까지 기다린다."""
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id=TEST_ACCESS_KEY,
        aws_secret_access_key=TEST_ACCESS_KEY,
    )
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            client.create_bucket(Bucket=bucket)
            break
        except (BotoCoreError, ClientError):
            # 컨테이너가 떠도 S3 게이트웨이는 몇 초 뒤에 열린다.
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    # 버킷이 생겨도 볼륨 서버가 준비되기 전에는 PutObject가 InternalError로 실패한다(CI에서 확인).
    while True:
        try:
            client.put_object(Bucket=bucket, Key=".ready", Body=b"")
            client.delete_object(Bucket=bucket, Key=".ready")
            return
        except (BotoCoreError, ClientError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@contextmanager
def postgres() -> Iterator[str]:
    """PostgreSQL을 띄우고 SQLAlchemy asyncpg 접속 URL을 돌려준다."""
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


@contextmanager
def redis() -> Iterator[str]:
    """Redis를 띄우고 `redis://` 접속 URL을 돌려준다."""
    container = DockerContainer(REDIS_IMAGE).with_exposed_ports(6379)
    container.waiting_for(LogMessageWaitStrategy("Ready to accept connections"))
    with container:
        yield f"redis://{container.get_container_host_ip()}:{container.get_exposed_port(6379)}/0"
