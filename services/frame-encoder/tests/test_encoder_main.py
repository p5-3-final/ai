from pathlib import Path

import pytest
from fe_support import ENCODER_MANIFEST_KEY, put_encoder_model
from frame_encoder.__main__ import prepare
from frame_encoder.settings import FrameEncoderSettings
from frame_encoder.vectors import normalized_mean
from vlm_common.profile import ProfileError
from vlm_encoders.base import EncoderLoadError
from vlm_storage.memory import InMemoryStorage

OPEN_CLIP = f"""
[encoder]
kind = "open_clip"
model_id = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
dimension = 512
manifest = "{ENCODER_MANIFEST_KEY}"
manifest_sha256 = "{{sha}}"
"""
MOCK = """
[encoder]
kind = "mock"
model_id = "mock:mock:mock:mean4"
dimension = 8
"""
PROTOTYPES = """
[encoding]
batch_size = 4

[prototypes]
version = "local-sample-v1"

[prototypes.types.fall]
texts = ["a worker lying on the floor", "a person fallen down"]
threshold = 0.25
"""


def _settings(tmp_path: Path, profile: str, *, allow_mock: bool = False) -> FrameEncoderSettings:
    path = tmp_path / "profile.toml"
    path.write_text(profile, encoding="utf-8")
    return FrameEncoderSettings(
        profile_path=path, model_cache_dir=tmp_path / "cache", allow_mock=allow_mock
    )


def test_spec_019_r9_manifest_dimension_mismatch_fails_startup(tmp_path: Path) -> None:
    models = InMemoryStorage()
    sha = put_encoder_model(models, b"weights", dimension=768)

    with pytest.raises(EncoderLoadError, match="dimension"):
        prepare(_settings(tmp_path, OPEN_CLIP.format(sha=sha)), models)


def test_spec_019_r9_manifest_hash_mismatch_fails_startup(tmp_path: Path) -> None:
    models = InMemoryStorage()
    put_encoder_model(models, b"weights")

    with pytest.raises(EncoderLoadError, match="manifest sha256"):
        prepare(_settings(tmp_path, OPEN_CLIP.format(sha="0" * 64)), models)


def test_open_clip_needs_models_bucket(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="bucket"):
        prepare(_settings(tmp_path, OPEN_CLIP.format(sha="0" * 64)), None)


def test_mock_encoder_fails_startup_unless_allowed(tmp_path: Path) -> None:
    # mock은 CI·로컬 전용이다. 운영에서 실수로 고르면 의미 없는 벡터가 검색 인덱스에 들어간다.
    with pytest.raises(ProfileError, match="mock"):
        prepare(_settings(tmp_path, MOCK), None)


def test_spec_019_r15_invalid_profile_fails_startup(tmp_path: Path) -> None:
    profile = MOCK + '[prototypes]\nversion = "v1"\n[prototypes.types.other]\ntexts = ["x"]\n'

    with pytest.raises(ProfileError):
        prepare(_settings(tmp_path, profile + "threshold = 0.2\n", allow_mock=True), None)


def test_spec_019_r10_builds_prototypes_once_at_startup(tmp_path: Path) -> None:
    ready = prepare(_settings(tmp_path, MOCK + PROTOTYPES, allow_mock=True), None)

    assert (ready.encoder.model_id, ready.encoder.dimension) == ("mock:mock:mock:mean4", 8)
    assert ready.config.batch_size == 4
    prototypes = ready.config.prototypes
    assert prototypes is not None
    assert prototypes.version == "local-sample-v1"
    expected = normalized_mean(
        ready.encoder.encode_texts(["a worker lying on the floor", "a person fallen down"])
    )
    assert prototypes.vector("fall") == pytest.approx(expected)


def test_profile_without_prototypes_makes_no_candidates(tmp_path: Path) -> None:
    ready = prepare(_settings(tmp_path, MOCK, allow_mock=True), None)

    assert ready.config.prototypes is None
