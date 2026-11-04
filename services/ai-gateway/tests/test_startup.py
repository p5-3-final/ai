from pathlib import Path

import pytest
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.adapters.openai import OpenAiAdapter
from ai_gateway.main import build_adapters, check_tasks, load_encoder
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptRegistry
from ai_gateway.settings import GatewaySettings
from gateway_support import ENCODER_MANIFEST_KEY, profile, put_encoder_model, random_weights
from pydantic import SecretStr
from vlm_common.profile import ProfileError
from vlm_encoders.base import EncoderLoadError
from vlm_encoders.open_clip import OpenClipEncoder
from vlm_storage.memory import InMemoryStorage


def test_spec009_r14_profile_task_must_have_prompt(registry_dir: Path) -> None:
    registry = PromptRegistry.load(registry_dir)
    with_extra = profile().model_copy(
        update={"tasks": {**profile().tasks, "track_attribute": profile().tasks["event_judge"]}}
    )

    with pytest.raises(ProfileError, match="track_attribute"):
        check_tasks(with_extra, registry)


def test_spec009_r14_every_prompt_needs_profile_task(registry_dir: Path) -> None:
    registry = PromptRegistry.load(registry_dir)
    without = profile().model_copy(update={"tasks": {}})

    with pytest.raises(ProfileError, match="event_judge"):
        check_tasks(without, registry)


def test_spec009_r14_matching_profile_passes(registry_dir: Path) -> None:
    check_tasks(profile(), PromptRegistry.load(registry_dir))


def test_spec009_r1_builds_adapter_per_profile_entry(registry_dir: Path) -> None:
    gateway_profile = profile().model_copy(
        update={
            "adapters": {
                "fake": profile().adapters["fake"],
                "ollama": profile()
                .adapters["fake"]
                .model_copy(
                    update={"kind": "openai_compatible", "base_url": "http://ollama:11434/v1"}
                ),
            }
        }
    )

    adapters = build_adapters(gateway_profile, PromptRegistry.load(registry_dir), api_key=None)

    assert isinstance(adapters["fake"], MockAdapter)
    assert isinstance(adapters["ollama"], OpenAiAdapter)


def test_spec009_nfr4_api_key_is_not_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AI_GATEWAY_DATABASE_URL", "postgresql+asyncpg://u:secret-pw@db/ai")
    monkeypatch.setenv("AI_GATEWAY_PROFILE_PATH", "/p.toml")
    monkeypatch.setenv("AI_GATEWAY_OPENAI_API_KEY", "sk-very-secret")
    monkeypatch.setenv("KAFKA_BOOTSTRAP", "kafka:9092")

    settings = GatewaySettings()

    assert isinstance(settings.openai_api_key, SecretStr)
    assert "sk-very-secret" not in repr(settings)
    assert "secret-pw" not in repr(settings)


SHA = "0" * 64


def _encoder_profile(kind: str, sha256: str = SHA) -> GatewayProfile:
    encoder: dict[str, object] = {
        "kind": kind,
        "model_id": "open_clip:MobileCLIP2-S0:dfndr2b:mean-4",
        "dimension": 512,
    }
    if kind == "open_clip":
        encoder |= {"manifest": ENCODER_MANIFEST_KEY, "manifest_sha256": sha256}
    return profile({"encoder": encoder})


def test_spec011_r14_no_encoder_section_loads_nothing(tmp_path: Path) -> None:
    """[SPEC-011 R-14] [encoder]가 없으면 인코더를 만들지 않는다."""
    assert load_encoder(profile(), None, tmp_path) is None


def test_spec011_r15_mock_encoder_needs_no_weights(tmp_path: Path) -> None:
    """[SPEC-011 R-15] mock 인코더는 가중치 없이 프로필의 모델 ID·차원으로 만든다."""
    encoder = load_encoder(_encoder_profile("mock"), None, tmp_path)

    assert encoder is not None
    assert (encoder.model_id, encoder.dimension) == ("open_clip:MobileCLIP2-S0:dfndr2b:mean-4", 512)


def test_spec011_r16_open_clip_needs_models_bucket(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 모델 버킷 설정이 없으면 가중치를 받을 수 없어 기동에 실패한다."""
    with pytest.raises(ProfileError, match="bucket"):
        load_encoder(_encoder_profile("open_clip"), None, tmp_path)


def test_spec011_r16_manifest_hash_mismatch_fails_startup(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 받은 manifest의 해시가 프로필과 다르면 기동에 실패한다."""
    models = InMemoryStorage()
    put_encoder_model(models, b"weights")

    with pytest.raises(EncoderLoadError, match="manifest sha256"):
        load_encoder(_encoder_profile("open_clip"), models, tmp_path)


def test_spec011_r16_manifest_dimension_mismatch_fails_startup(tmp_path: Path) -> None:
    """[SPEC-011 R-16] manifest의 차원이 프로필 차원과 다르면 가중치를 로드하지 않는다."""
    models = InMemoryStorage()
    sha256 = put_encoder_model(models, b"weights", dimension=768)

    with pytest.raises(EncoderLoadError, match="dimension"):
        load_encoder(_encoder_profile("open_clip", sha256), models, tmp_path)


def test_spec011_r15_loads_open_clip_from_models_bucket(tmp_path: Path) -> None:
    """[SPEC-011 R-15] 모델 버킷의 manifest가 가리키는 가중치로 open_clip 인코더를 로드한다."""
    models = InMemoryStorage()
    sha256 = put_encoder_model(models, random_weights())

    encoder = load_encoder(_encoder_profile("open_clip", sha256), models, tmp_path)

    assert isinstance(encoder, OpenClipEncoder)
    assert len(encoder.encode_texts(["a forklift"])[0]) == 512
