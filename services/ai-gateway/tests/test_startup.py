from pathlib import Path

import pytest
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.adapters.openai import OpenAiAdapter
from ai_gateway.main import build_adapters, check_tasks
from ai_gateway.prompts import PromptRegistry
from ai_gateway.settings import GatewaySettings
from gateway_support import profile
from pydantic import SecretStr
from vlm_common.profile import ProfileError


def test_spec009_r14_profile_task_must_have_prompt(contracts_dir: Path) -> None:
    registry = PromptRegistry.load(contracts_dir)
    with_extra = profile().model_copy(
        update={"tasks": {**profile().tasks, "track_attribute": profile().tasks["event_judge"]}}
    )

    with pytest.raises(ProfileError, match="track_attribute"):
        check_tasks(with_extra, registry)


def test_spec009_r14_every_prompt_needs_profile_task(contracts_dir: Path) -> None:
    registry = PromptRegistry.load(contracts_dir)
    without = profile().model_copy(update={"tasks": {}})

    with pytest.raises(ProfileError, match="event_judge"):
        check_tasks(without, registry)


def test_spec009_r14_matching_profile_passes(contracts_dir: Path) -> None:
    check_tasks(profile(), PromptRegistry.load(contracts_dir))


def test_spec009_r1_builds_adapter_per_profile_entry() -> None:
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

    adapters = build_adapters(gateway_profile, api_key=None)

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
