import time
import uuid
from collections.abc import Iterator

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from testcontainers.core.container import DockerContainer
from vlm_storage.memory import InMemoryStorage
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

# 로컬 compose(local/docker-compose.yml)와 같은 이미지로 검증한다.
SEAWEEDFS_IMAGE = (
    "chrislusf/seaweedfs:4.48"
    "@sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d"
)


@pytest.fixture(scope="session")
def seaweedfs_endpoint() -> Iterator[str]:
    # 접근 키 설정 없이 띄우면 SeaweedFS S3는 서명을 검사하지 않는다. 테스트 키는 아무 값이나 된다.
    container = DockerContainer(SEAWEEDFS_IMAGE).with_command("server -s3 -dir=/data")
    container.with_exposed_ports(8333)
    with container:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(8333)}"
        yield endpoint


def _settings(endpoint: str, bucket: str) -> StorageSettings:
    return StorageSettings(endpoint_url=endpoint, bucket=bucket, path_style=True)


def _create_bucket(endpoint: str, bucket: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",  # noqa: S106  # 서명을 검사하지 않는 테스트 컨테이너용 값 (#8)
    )
    deadline = time.monotonic() + 60
    while True:
        try:
            client.create_bucket(Bucket=bucket)
            return
        except (BotoCoreError, ClientError):
            # 컨테이너가 떠도 S3 게이트웨이는 몇 초 뒤에 열린다.
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


@pytest.fixture(params=["s3", "memory"])
def storage(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> ObjectStorage:
    if request.param == "memory":
        return InMemoryStorage()
    endpoint: str = request.getfixturevalue("seaweedfs_endpoint")
    bucket = f"test-{uuid.uuid4().hex[:12]}"
    _create_bucket(endpoint, bucket)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    return S3Storage(_settings(endpoint, bucket))
