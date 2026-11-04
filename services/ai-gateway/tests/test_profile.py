from pathlib import Path

import pytest
from ai_gateway.main import check_tasks
from ai_gateway.profile import GatewayProfile, check_profile
from ai_gateway.prompts import PromptRegistry
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


@pytest.mark.parametrize("name", ["local.toml", "local-mock.toml"])
def test_spec009_r14_committed_profiles_are_valid(registry_dir: Path, name: str) -> None:
    path = Path(__file__).resolve().parents[1] / "profiles" / name

    profile = load_profile(path, GatewayProfile)

    check_profile(profile, allow_mock=True, has_openai_key=False)
    check_tasks(profile, PromptRegistry.load(registry_dir))


SHA = "ab91a1a0c4330d6b1913e24d5035dfdea15423316aaec649610c6b1c6ddd0e95"
EMBED = f"""
[encoder]
kind = "open_clip"
model_id = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
dimension = 512
weights_key = "open_clip/MobileCLIP2-S0/dfndr2b/open_clip_model.safetensors"
weights_sha256 = "{SHA}"

[embed.embed_visual_text]
max_inputs = 16
max_text_chars = 300

[embed.embed_image]
max_inputs = 8
allowed_patterns = ['^queries/[^/]+/[^/]+\\.(jpg|png)$']

[embed.embed_text]
adapter = "ollama"
model = "bge-m3"
dimension = 1024
max_inputs = 64
max_text_chars = 2000
timeout_s = 10
max_retries = 2
"""


def test_spec011_r14_embed_profile_loads(tmp_path: Path) -> None:
    """[SPEC-011 R-14] 인코더와 taskType별 임베딩 설정을 읽는다."""
    profile = _load(tmp_path, VALID + EMBED)

    check_profile(profile, allow_mock=False, has_openai_key=False)
    assert profile.encoder is not None
    assert (profile.encoder.model_id, profile.encoder.dimension) == (
        "open_clip:MobileCLIP2-S0:dfndr2b:mean-4",
        512,
    )
    text = profile.embed.embed_text
    assert text is not None
    assert (text.adapter, text.model, text.dimension, text.max_inputs) == (
        "ollama",
        "bge-m3",
        1024,
        64,
    )
    assert profile.embed.embed_image is not None
    assert profile.embed.embed_image.max_inputs == 8


def test_spec011_r14_embed_is_optional(tmp_path: Path) -> None:
    """[SPEC-011 R-14] 임베딩 설정이 없는 프로필도 뜬다(모든 임베딩 taskType이 모델 미설정)."""
    profile = _load(tmp_path, VALID)

    assert profile.encoder is None
    assert profile.embed.embed_text is None


def test_spec011_r14_embed_text_without_model_is_not_configured(tmp_path: Path) -> None:
    """[SPEC-011 R-14] embed_text의 Adapter·모델·차원을 빼면 모델 미설정이다."""
    text = EMBED.replace('adapter = "ollama"\nmodel = "bge-m3"\ndimension = 1024\n', "")

    profile = _load(tmp_path, VALID + text)

    assert profile.embed.embed_text is not None
    assert profile.embed.embed_text.model is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # 알 수 없는 임베딩 taskType
        ("[embed.embed_text]", "[embed.embed_audio]"),
        # 모르는 Adapter를 가리킴
        ('adapter = "ollama"\nmodel = "bge-m3"', 'adapter = "nope"\nmodel = "bge-m3"'),
        # Adapter·모델·차원은 함께 있어야 한다
        ('model = "bge-m3"\ndimension = 1024\n', 'model = "bge-m3"\n'),
        # open_clip 인코더는 가중치 키와 해시가 있어야 한다
        (f'weights_sha256 = "{SHA}"\n', ""),
        (f'weights_sha256 = "{SHA}"', 'weights_sha256 = "abc"'),
        ('model_id = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"', 'model_id = "MobileCLIP2-S0"'),
        # 경로 패턴이 정규식이 아님
        ("allowed_patterns = ['^queries/[^/]+/[^/]+\\.(jpg|png)$']", "allowed_patterns = ['(']"),
        ("max_inputs = 16", "max_inputs = 0"),
    ],
)
def test_spec011_r14_invalid_embed_profile_fails(tmp_path: Path, old: str, new: str) -> None:
    """[SPEC-011 R-14] 잘못된 임베딩 설정이면 기동에 실패한다."""
    assert old in EMBED
    with pytest.raises(ProfileError):
        _load(tmp_path, VALID + EMBED.replace(old, new))


MOCK_ENCODER = """
[encoder]
kind = "mock"
model_id = "mock:mock:mock:mean-4"
dimension = 8
"""


def test_spec011_r24_mock_encoder_requires_allow_mock(tmp_path: Path) -> None:
    """[SPEC-011 R-24] mock 허용 설정 없이 mock 인코더를 고르면 기동에 실패한다."""
    profile = _load(tmp_path, VALID + MOCK_ENCODER)

    with pytest.raises(ProfileError, match="mock"):
        check_profile(profile, allow_mock=False, has_openai_key=False)
    check_profile(profile, allow_mock=True, has_openai_key=False)


def test_spec011_r24_mock_text_embedding_adapter_requires_allow_mock(tmp_path: Path) -> None:
    """[SPEC-011 R-24] embed_text가 mock Adapter를 쓰는 것도 같은 규칙이다."""
    text = (
        VALID
        + '\n[adapters.fake]\nkind = "mock"\n'
        + EMBED.replace('adapter = "ollama"\nmodel = "bge-m3"', 'adapter = "fake"\nmodel = "mock"')
    )
    profile = _load(tmp_path, text)

    with pytest.raises(ProfileError, match="mock"):
        check_profile(profile, allow_mock=False, has_openai_key=False)
