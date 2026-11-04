import hashlib
import json
from pathlib import Path

import pytest
from vlm_encoders.base import EncoderLoadError
from vlm_encoders.weights import fetch_encoder_weights
from vlm_storage.memory import InMemoryStorage

PREFIX = "models/mobileclip2-s0/dfndr2b"
MANIFEST_KEY = f"{PREFIX}/manifest.json"
MODEL_ID = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
DATA = b"weights"


def _file(name: str) -> dict[str, object]:
    return {"name": name, "sha256": hashlib.sha256(DATA).hexdigest(), "size": len(DATA)}


def _manifest(**changes: object) -> dict[str, object]:
    return {
        "kind": "image_text_encoder",
        "modelId": "open_clip:MobileCLIP2-S0:dfndr2b",
        "version": "dfndr2b",
        "files": [_file("open_clip_model.safetensors")],
        "dimension": 512,
        "source": {"type": "public", "origin": "huggingface:timm/MobileCLIP2-S0-OpenCLIP"},
        "license": "Apple AMLR",
    } | changes


def _fetch(tmp_path: Path, manifest: dict[str, object], dimension: int = 512) -> Path:
    raw = json.dumps(manifest).encode()
    storage = InMemoryStorage()
    storage.put_bytes(MANIFEST_KEY, raw, "application/json")
    storage.put_bytes(f"{PREFIX}/open_clip_model.safetensors", DATA, "application/octet-stream")
    storage.put_bytes(f"{PREFIX}/extra.bin", DATA, "application/octet-stream")
    sha = hashlib.sha256(raw).hexdigest()
    return fetch_encoder_weights(storage, MANIFEST_KEY, sha, MODEL_ID, dimension, tmp_path)


def test_spec011_r15_returns_weights_file_of_matching_manifest(tmp_path: Path) -> None:
    """[SPEC-011 R-15] manifest가 프로필과 맞으면 해시를 확인한 가중치 파일 경로를 돌려준다."""
    path = _fetch(tmp_path, _manifest())

    assert path.read_bytes() == DATA
    assert path.name == "open_clip_model.safetensors"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"kind": "detector", "classes": ["person"], "dimension": None}, "kind"),
        ({"modelId": "open_clip:MobileCLIP2-S2:dfndr2b"}, "model id"),
        ({"dimension": 768}, "dimension"),
    ],
)
def test_spec011_r16_rejects_manifest_not_matching_profile(
    tmp_path: Path, changes: dict[str, object], message: str
) -> None:
    """[SPEC-011 R-16] manifest의 모델 종류·모델 ID·차원이 프로필과 다르면 실패한다."""
    manifest = {k: v for k, v in _manifest(**changes).items() if v is not None}

    with pytest.raises(EncoderLoadError, match=message):
        _fetch(tmp_path, manifest)


def test_spec011_r16_rejects_more_than_one_weights_file(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 인코더는 가중치 파일 하나를 읽으므로 파일이 여럿이면 실패한다."""
    files = [_file("open_clip_model.safetensors"), _file("extra.bin")]

    with pytest.raises(EncoderLoadError, match="one weights file"):
        _fetch(tmp_path, _manifest(files=files))


def test_spec011_r16_wraps_artifact_errors(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 모델 저장소에 manifest가 없으면 인코더 로드 오류로 실패한다."""
    with pytest.raises(EncoderLoadError, match="manifest not found"):
        fetch_encoder_weights(InMemoryStorage(), MANIFEST_KEY, "0" * 64, MODEL_ID, 512, tmp_path)
