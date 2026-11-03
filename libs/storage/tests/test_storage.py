"""S3Storage(SeaweedFS)와 InMemoryStorage가 같은 동작을 하는지 검증한다."""

from pathlib import Path

import pytest
from vlm_storage.s3 import StorageSettings
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

pytestmark = pytest.mark.integration


def test_bytes_round_trip(storage: ObjectStorage) -> None:
    storage.put_bytes("sources/a/meta.json", b'{"ok": true}', "application/json")

    assert storage.get_bytes("sources/a/meta.json") == b'{"ok": true}'


def test_file_round_trip(storage: ObjectStorage, tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    source.write_bytes(b"\x00\x01" * 1000)
    target = tmp_path / "nested" / "out.mp4"

    storage.put_file("sources/a/normalized.mp4", source, "video/mp4")
    storage.get_file("sources/a/normalized.mp4", target)

    assert target.read_bytes() == source.read_bytes()


def test_exists_reflects_presence(storage: ObjectStorage) -> None:
    assert not storage.exists("sources/a/x")

    storage.put_bytes("sources/a/x", b"1", "application/octet-stream")

    assert storage.exists("sources/a/x")


def test_missing_object_raises_not_found(storage: ObjectStorage, tmp_path: Path) -> None:
    with pytest.raises(ObjectNotFoundError, match="sources/none"):
        storage.get_bytes("sources/none")
    with pytest.raises(ObjectNotFoundError):
        storage.get_file("sources/none", tmp_path / "x")


def test_overwrite_replaces_content(storage: ObjectStorage) -> None:
    # 결정적 경로에 다시 쓰는 재처리가 같은 결과를 남기는지 본다.
    storage.put_bytes("k", b"old", "text/plain")
    storage.put_bytes("k", b"new", "text/plain")

    assert storage.get_bytes("k") == b"new"


def test_delete_prefix_removes_only_that_prefix(storage: ObjectStorage) -> None:
    for key in ["sources/a/1", "sources/a/hls/2", "sources/ab/3"]:
        storage.put_bytes(key, b"x", "application/octet-stream")

    deleted = storage.delete_prefix("sources/a/")

    assert deleted == 2
    assert not storage.exists("sources/a/1")
    assert not storage.exists("sources/a/hls/2")
    assert storage.exists("sources/ab/3")


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S3_BUCKET", "vlm-media")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://seaweedfs:8333")
    monkeypatch.setenv("S3_PATH_STYLE", "true")

    settings = StorageSettings()

    assert settings.bucket == "vlm-media"
    assert settings.endpoint_url == "http://seaweedfs:8333"
    assert settings.path_style
    assert settings.region == "us-east-1"
