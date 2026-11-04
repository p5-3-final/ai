import hashlib
import json
from pathlib import Path

import pytest
from vlm_storage.artifacts import ArtifactError, fetch_model
from vlm_storage.memory import InMemoryStorage

PREFIX = "models/mobileclip2-s0/dfndr2b"
MANIFEST_KEY = f"{PREFIX}/manifest.json"
WEIGHTS = b"weights"


def _manifest(weights_sha256: str | None = None) -> bytes:
    manifest = {
        "kind": "image_text_encoder",
        "modelId": "open_clip:MobileCLIP2-S0:dfndr2b",
        "version": "dfndr2b",
        "files": [
            {
                "name": "open_clip_model.safetensors",
                "sha256": weights_sha256 or hashlib.sha256(WEIGHTS).hexdigest(),
                "size": len(WEIGHTS),
            }
        ],
        "dimension": 512,
        "source": {"type": "public", "origin": "huggingface:timm/MobileCLIP2-S0-OpenCLIP"},
        "license": "Apple AMLR",
    }
    return json.dumps(manifest).encode()


MANIFEST = _manifest()
MANIFEST_SHA = hashlib.sha256(MANIFEST).hexdigest()


def _storage(manifest: bytes = MANIFEST, weights: bytes | None = WEIGHTS) -> InMemoryStorage:
    s = InMemoryStorage()
    s.put_bytes(MANIFEST_KEY, manifest, "application/json")
    if weights is not None:
        s.put_bytes(f"{PREFIX}/open_clip_model.safetensors", weights, "application/octet-stream")
    return s


def test_spec011_r15_downloads_manifest_and_files(tmp_path: Path) -> None:
    """[SPEC-011 R-15] manifest 해시를 확인하고, 적힌 파일을 같은 폴더에서 받아 경로를 돌려준다."""
    fetched = fetch_model(_storage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path)

    assert fetched.manifest.model_id == "open_clip:MobileCLIP2-S0:dfndr2b"
    assert fetched.files["open_clip_model.safetensors"].read_bytes() == WEIGHTS


def test_spec011_r16_rejects_manifest_hash_mismatch(tmp_path: Path) -> None:
    """[SPEC-011 R-16] manifest 해시가 프로필과 다르면 파일을 받지 않고 실패한다."""
    with pytest.raises(ArtifactError, match="manifest sha256"):
        fetch_model(_storage(), MANIFEST_KEY, "0" * 64, tmp_path)

    assert not [p for p in tmp_path.rglob("*") if p.is_file()]


def test_spec011_r16_rejects_file_hash_mismatch(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 파일 해시가 manifest와 다르면 실패하고 받은 파일을 남기지 않는다."""
    manifest = _manifest("0" * 64)

    with pytest.raises(ArtifactError, match=r"open_clip_model\.safetensors"):
        fetch_model(
            _storage(manifest), MANIFEST_KEY, hashlib.sha256(manifest).hexdigest(), tmp_path
        )

    assert not [p for p in tmp_path.rglob("*.safetensors*") if p.is_file()]


def test_spec011_r16_rejects_missing_manifest(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 모델 저장소에 manifest가 없으면 실패한다."""
    with pytest.raises(ArtifactError, match="not found"):
        fetch_model(InMemoryStorage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path)


def test_spec011_r16_rejects_missing_file(tmp_path: Path) -> None:
    """[SPEC-011 R-16] manifest에 적힌 파일이 없으면 실패한다."""
    with pytest.raises(ArtifactError, match="not found"):
        fetch_model(_storage(weights=None), MANIFEST_KEY, MANIFEST_SHA, tmp_path)


def test_spec011_r16_rejects_invalid_manifest(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 계약 형식이 아닌 manifest는 해시가 맞아도 실패한다."""
    broken = b'{"kind": "image_text_encoder"}'

    with pytest.raises(ArtifactError, match="invalid manifest"):
        fetch_model(_storage(broken), MANIFEST_KEY, hashlib.sha256(broken).hexdigest(), tmp_path)


def test_spec011_r18_reuses_cache_without_download(tmp_path: Path) -> None:
    """[SPEC-011 R-18] 캐시에 해시가 맞는 manifest와 파일이 있으면 저장소를 읽지 않는다."""
    first = fetch_model(_storage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path)

    # 저장소가 비어 있어도 캐시로 끝난다.
    assert fetch_model(InMemoryStorage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path) == first


def test_spec011_r18_downloads_again_when_cached_file_is_corrupted(tmp_path: Path) -> None:
    """[SPEC-011 R-18] 캐시 파일의 해시가 manifest와 다르면 그 파일을 다시 받는다."""
    path = fetch_model(_storage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path).files[
        "open_clip_model.safetensors"
    ]
    path.write_bytes(b"broken")

    fetched = fetch_model(_storage(), MANIFEST_KEY, MANIFEST_SHA, tmp_path)

    assert fetched.files["open_clip_model.safetensors"].read_bytes() == WEIGHTS
