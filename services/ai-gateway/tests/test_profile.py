from pathlib import Path

import pytest
from ai_gateway.profile import GatewayProfile, check_profile
from vlm_common.profile import ProfileError, load_profile

VALID = """
[callers]
allowed = ["event-analyzer"]

[images]
allowed_patterns = ['^sources/{source_id}/.+/blurred/[^/]+\\.jpg$']

[adapters.ollama]
kind = "openai_compatible"
base_url = "http://ollama:11434/v1"

[tasks.event_judge]
adapter = "ollama"
model = "qwen3-vl:8b"
max_output_tokens = 800
max_images = 12
timeout_s = 30
max_retries = 2
"""


OLLAMA = 'kind = "openai_compatible"\nbase_url = "http://ollama:11434/v1"'


def _load(tmp_path: Path, text: str) -> GatewayProfile:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return load_profile(path, GatewayProfile)


def test_spec009_r14_valid_profile_loads(tmp_path: Path) -> None:
    profile = _load(tmp_path, VALID)

    check_profile(profile, allow_mock=False, has_openai_key=False)
    task = profile.tasks["event_judge"]
    assert (task.adapter, task.model, task.max_images) == ("ollama", "qwen3-vl:8b", 12)


def test_spec009_r14_task_without_model_is_not_configured(tmp_path: Path) -> None:
    text = VALID.replace('adapter = "ollama"\nmodel = "qwen3-vl:8b"\n', "")

    profile = _load(tmp_path, text)

    check_profile(profile, allow_mock=False, has_openai_key=False)
    assert profile.tasks["event_judge"].model is None


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("모르는 Adapter 참조", 'adapter = "ollama"', 'adapter = "nope"'),
        ("Adapter만 있고 모델 없음", 'model = "qwen3-vl:8b"\n', ""),
        ("openai_compatible인데 주소 없음", 'base_url = "http://ollama:11434/v1"\n', ""),
        ("모르는 Adapter 종류", 'kind = "openai_compatible"', 'kind = "anthropic"'),
        ("잘못된 정규식", "blurred/[^/]+", "blurred/[^/+"),
        ("이미지 상한 0", "max_images = 12", "max_images = 0"),
        ("호출자 이름 형식", '"event-analyzer"', '"Event Analyzer"'),
        ("모르는 키", "max_retries = 2", "max_retries = 2\nretries = 3"),
    ],
)
def test_spec009_r14_invalid_profile_fails(tmp_path: Path, name: str, old: str, new: str) -> None:
    assert old in VALID, name
    with pytest.raises(ProfileError):
        check_profile(
            _load(tmp_path, VALID.replace(old, new)), allow_mock=True, has_openai_key=True
        )


def test_spec009_r14_openai_adapter_requires_api_key(tmp_path: Path) -> None:
    text = VALID.replace(
        'kind = "openai_compatible"\nbase_url = "http://ollama:11434/v1"', 'kind = "openai"'
    )
    profile = _load(tmp_path, text)

    with pytest.raises(ProfileError, match="API key"):
        check_profile(profile, allow_mock=False, has_openai_key=False)
    check_profile(profile, allow_mock=False, has_openai_key=True)


def test_spec009_r15_mock_requires_explicit_allow(tmp_path: Path) -> None:
    text = VALID.replace(
        'kind = "openai_compatible"\nbase_url = "http://ollama:11434/v1"', 'kind = "mock"'
    )
    profile = _load(tmp_path, text)

    with pytest.raises(ProfileError, match="mock"):
        check_profile(profile, allow_mock=False, has_openai_key=False)
    check_profile(profile, allow_mock=True, has_openai_key=False)
