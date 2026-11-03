import uuid
from collections.abc import Iterator

import pytest
from vlm_storage.memory import InMemoryStorage
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage
from vlm_testing import containers


@pytest.fixture(scope="session")
def seaweedfs_endpoint() -> Iterator[str]:
    with containers.seaweedfs() as endpoint:
        yield endpoint


def _settings(endpoint: str, bucket: str) -> StorageSettings:
    return StorageSettings(endpoint_url=endpoint, bucket=bucket, path_style=True)


@pytest.fixture(params=["s3", "memory"])
def storage(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> ObjectStorage:
    if request.param == "memory":
        return InMemoryStorage()
    endpoint: str = request.getfixturevalue("seaweedfs_endpoint")
    bucket = f"test-{uuid.uuid4().hex[:12]}"
    containers.create_bucket(endpoint, bucket)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", containers.TEST_ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", containers.TEST_ACCESS_KEY)
    return S3Storage(_settings(endpoint, bucket))
