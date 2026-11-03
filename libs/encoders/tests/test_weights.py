import hashlib
from pathlib import Path

import pytest
from vlm_encoders.base import EncoderLoadError
from vlm_encoders.weights import fetch_weights
from vlm_storage.memory import InMemoryStorage

KEY = "open_clip/MobileCLIP2-S0/dfndr2b/open_clip_model.safetensors"
DATA = b"weights"
SHA = hashlib.sha256(DATA).hexdigest()


def test_spec011_r15_downloads_weights_with_matching_hash(tmp_path: Path) -> None:
    """[SPEC-011 R-15] 모델 저장소에서 받아 해시가 맞는 로컬 파일을 돌려준다."""
    path = fetch_weights(_storage(), KEY, SHA, tmp_path)

    assert path.read_bytes() == DATA
    assert path.name == "open_clip_model.safetensors"


def test_spec011_r16_rejects_hash_mismatch(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 해시가 프로필과 다르면 실패하고 받은 파일을 남기지 않는다."""
    with pytest.raises(EncoderLoadError, match="sha256"):
        fetch_weights(_storage(), KEY, "0" * 64, tmp_path)

    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_spec011_r16_rejects_missing_weights(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 모델 저장소에 가중치가 없으면 실패한다."""
    with pytest.raises(EncoderLoadError, match="not found"):
        fetch_weights(InMemoryStorage(), KEY, SHA, tmp_path)


def test_spec011_r18_reuses_cached_file_without_download(tmp_path: Path) -> None:
    """[SPEC-011 R-18] 캐시에 해시가 같은 파일이 있으면 저장소를 읽지 않는다."""
    first = fetch_weights(_storage(), KEY, SHA, tmp_path)

    # 저장소가 비어 있어도 캐시로 끝난다.
    assert fetch_weights(InMemoryStorage(), KEY, SHA, tmp_path) == first


def test_spec011_r18_downloads_again_when_cache_is_corrupted(tmp_path: Path) -> None:
    """[SPEC-011 R-18] 캐시 파일의 해시가 다르면 다시 받는다."""
    path = fetch_weights(_storage(), KEY, SHA, tmp_path)
    path.write_bytes(b"broken")

    assert fetch_weights(_storage(), KEY, SHA, tmp_path).read_bytes() == DATA


def _storage() -> InMemoryStorage:
    s = InMemoryStorage()
    s.put_bytes(KEY, DATA, "application/octet-stream")
    return s
