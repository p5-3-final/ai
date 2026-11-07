"""읽기 전용 서명 주소(SPEC-026). ffmpeg가 정규화 사본의 구간만 HTTP 범위 요청으로 읽는다."""

import httpx
import pytest
from vlm_storage.memory import InMemoryStorage
from vlm_storage.storage import ObjectStorage


@pytest.mark.integration
@pytest.mark.parametrize("storage", ["s3"], indirect=True)
def test_s3_presigned_url_reads_the_object_without_credentials(storage: ObjectStorage) -> None:
    storage.put_bytes("sources/a/normalized.mp4", b"0123456789", "video/mp4")

    url = storage.presigned_get_url("sources/a/normalized.mp4", expires_s=60)
    response = httpx.get(url, headers={"Range": "bytes=2-4"})

    assert response.status_code in {200, 206}
    assert response.content in {b"234", b"0123456789"}


@pytest.mark.integration
@pytest.mark.parametrize("storage", ["s3"], indirect=True)
def test_s3_presigned_url_of_missing_object_is_not_found(storage: ObjectStorage) -> None:
    url = storage.presigned_get_url("sources/a/missing.mp4", expires_s=60)

    assert httpx.get(url).status_code in {403, 404}


def test_memory_presigned_url_names_the_key() -> None:
    url = InMemoryStorage().presigned_get_url("sources/a/normalized.mp4", expires_s=60)

    assert url == "memory://sources/a/normalized.mp4"


@pytest.mark.parametrize("expires_s", [0, -1])
def test_presigned_url_requires_positive_expiry(expires_s: int) -> None:
    with pytest.raises(ValueError, match="expires_s"):
        InMemoryStorage().presigned_get_url("k", expires_s=expires_s)
