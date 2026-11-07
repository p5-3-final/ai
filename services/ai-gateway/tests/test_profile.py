from decimal import Decimal
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

[parse]
max_query_chars = 200
max_output_tokens = 200
timeout_s = 10
max_retries = 0
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
manifest = "models/mobileclip2-s0/dfndr2b/manifest.json"
manifest_sha256 = "{SHA}"

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
        # open_clip 인코더는 manifest 경로와 해시가 있어야 한다
        (f'manifest_sha256 = "{SHA}"\n', ""),
        (f'manifest_sha256 = "{SHA}"', 'manifest_sha256 = "abc"'),
        # manifest는 모델 저장소 규약 경로(models/<종류>/<버전>/manifest.json)여야 한다
        (
            '"models/mobileclip2-s0/dfndr2b/manifest.json"',
            '"open_clip/MobileCLIP2-S0/x.safetensors"',
        ),
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


PARSE_LLM = '[parse]\nadapter = "ollama"\nmodel = "qwen3-vl:4b-instruct"\n'


def test_spec012_r12_parse_without_model_uses_dictionary_only(tmp_path: Path) -> None:
    """[SPEC-012 R-12] [parse]에 모델이 없으면 LLM 보조 없이 사전만 쓴다."""
    profile = _load(tmp_path, VALID)

    check_profile(profile, allow_mock=False, has_openai_key=False)
    assert (profile.parse.adapter, profile.parse.model) == (None, None)
    assert profile.parse.max_query_chars == 200


def test_spec012_r10_parse_model_loads(tmp_path: Path) -> None:
    """[SPEC-012 R-10] [parse]의 Adapter·모델로 query_parse를 부른다."""
    profile = _load(tmp_path, VALID.replace("[parse]\n", PARSE_LLM))

    check_profile(profile, allow_mock=False, has_openai_key=False)
    assert (profile.parse.adapter, profile.parse.model) == ("ollama", "qwen3-vl:4b-instruct")


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("[parse] 없음", "[parse]\nmax_query_chars = 200\n", "[other]\n"),
        ("길이 상한 0", "max_query_chars = 200", "max_query_chars = 0"),
        ("모르는 Adapter 참조", "[parse]\n", '[parse]\nadapter = "nope"\nmodel = "m"\n'),
        ("Adapter만 있고 모델 없음", "[parse]\n", '[parse]\nadapter = "ollama"\n'),
    ],
)
def test_spec012_r21_invalid_parse_profile_fails(
    tmp_path: Path, name: str, old: str, new: str
) -> None:
    """[SPEC-012 R-21] [parse]의 길이 상한과 Adapter 참조를 검사한다."""
    assert old in VALID, name
    with pytest.raises(ProfileError):
        check_profile(
            _load(tmp_path, VALID.replace(old, new)), allow_mock=True, has_openai_key=True
        )


def test_spec012_r28_mock_parse_adapter_requires_allow_mock(tmp_path: Path) -> None:
    """[SPEC-012 R-28] query_parse가 mock Adapter를 쓰면 mock 허용 설정이 있어야 한다."""
    text = (
        VALID.replace("[parse]\n", '[parse]\nadapter = "fake"\nmodel = "mock"\n')
        + '\n[adapters.fake]\nkind = "mock"\n'
    )
    profile = _load(tmp_path, text)

    with pytest.raises(ProfileError, match="mock"):
        check_profile(profile, allow_mock=False, has_openai_key=False)
    check_profile(profile, allow_mock=True, has_openai_key=False)


GUARDS = """
[cache]
retention_days = 7
front_ttl_s = 600

[budget]
daily_limit_usd = 20.0

[budget.callers]
event-analyzer = 5.0

[prices.ollama."qwen3-vl:8b"]
input_per_million_usd = 0.4
output_per_million_usd = 1.6

[rate_limits.ollama."qwen3-vl:8b"]
rpm = 60
tpm = 100000
pod_rpm = 30
"""


def test_spec016_r20_guard_profile_loads(tmp_path: Path) -> None:
    """[SPEC-016 R-20] 캐시·예산·단가·분당 한도를 프로필로 둔다."""
    profile = _load(
        tmp_path,
        VALID.replace("max_retries = 2\n", "max_retries = 2\nestimated_input_tokens = 3000\n", 1)
        + GUARDS,
    )

    check_profile(profile, allow_mock=False, has_openai_key=False)
    assert (profile.cache.retention_days, profile.cache.front_ttl_s) == (7, 600)
    assert profile.budget is not None
    assert profile.budget.daily_limit_usd == Decimal("20.0")
    assert profile.budget.callers == {"event-analyzer": Decimal("5.0")}
    price = profile.price_of("ollama", "qwen3-vl:8b")
    assert price is not None
    assert price.input_per_million_usd == Decimal("0.4")
    limit = profile.rate_limit_of("ollama", "qwen3-vl:8b")
    assert limit is not None
    assert (limit.rpm, limit.tpm, limit.pod_rpm, limit.pod_tpm) == (60, 100000, 30, 100000)
    assert profile.tasks["event_judge"].estimated_input_tokens == 3000


def test_spec016_r20_guards_are_optional(tmp_path: Path) -> None:
    """[SPEC-016 R-20] 없으면 기본 보관 기간, 예산·단가·분당 한도 없음."""
    profile = _load(tmp_path, VALID)

    assert profile.budget is None
    assert profile.price_of("ollama", "qwen3-vl:8b") is None
    assert profile.rate_limit_of("ollama", "qwen3-vl:8b") is None
    assert profile.cache.retention_days > 0
    assert profile.tasks["event_judge"].estimated_input_tokens == 0


@pytest.mark.parametrize(
    ("name", "old", "new"),
    [
        ("모르는 Adapter의 단가", '[prices.ollama."qwen3-vl:8b"]', '[prices.nope."qwen3-vl:8b"]'),
        ("쓰지 않는 모델의 단가", '[prices.ollama."qwen3-vl:8b"]', '[prices.ollama."other"]'),
        (
            "모르는 Adapter의 분당 한도",
            '[rate_limits.ollama."qwen3-vl:8b"]',
            '[rate_limits.nope."m"]',
        ),
        ("허용 목록 밖 호출 서비스 예산", "event-analyzer = 5.0", "stranger = 5.0"),
        ("음수 한도", "daily_limit_usd = 20.0", "daily_limit_usd = -1.0"),
        ("음수 단가", "input_per_million_usd = 0.4", "input_per_million_usd = -0.4"),
        ("분당 한도 0", "rpm = 60", "rpm = 0"),
        ("보관 기간 0", "retention_days = 7", "retention_days = 0"),
        ("모르는 키", "front_ttl_s = 600", "front_ttl_s = 600\nttl = 3"),
    ],
)
def test_spec016_r20_invalid_guard_profile_fails(
    tmp_path: Path, name: str, old: str, new: str
) -> None:
    """[SPEC-016 R-20] 형식이 틀리거나 없는 Adapter·모델·호출 서비스를 가리키면 기동 실패."""
    assert old in GUARDS, name
    with pytest.raises(ProfileError):
        _load(tmp_path, VALID + GUARDS.replace(old, new))


def test_spec016_r21_caller_without_limit_uses_global_only(tmp_path: Path) -> None:
    """[SPEC-016 R-21] 호출 서비스 한도가 없으면 전체 한도만 적용한다."""
    profile = _load(tmp_path, VALID + GUARDS)

    assert profile.budget is not None
    assert profile.budget.limit_for("search-service") is None
    assert profile.budget.limit_for("event-analyzer") == Decimal("5.0")


def test_spec024_r11_min_images_defaults_to_zero(tmp_path: Path) -> None:
    """[SPEC-024 R-11] 하한을 적지 않은 기존 taskType은 이미지가 없어도 받는다(동작 유지)."""
    assert _load(tmp_path, VALID).tasks["event_judge"].min_images == 0


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("max_images = 12", "max_images = 12\nmin_images = -1"),
        ("max_images = 12", "max_images = 12\nmin_images = 13"),
    ],
)
def test_spec024_r11_invalid_min_images_fails(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ProfileError):
        _load(tmp_path, VALID.replace(old, new))


@pytest.mark.parametrize("task_type", ["track_attribute", "object_question"])
@pytest.mark.parametrize("name", ["local.toml", "local-mock.toml"])
def test_spec024_r11_r12_committed_profiles_need_one_image(name: str, task_type: str) -> None:
    path = Path(__file__).resolve().parents[1] / "profiles" / name

    assert load_profile(path, GatewayProfile).tasks[task_type].min_images == 1


PREDICTOR = """
[adapters.rules]
kind = "predictor"

[adapters.rules.predictor]
horizons_sec = [600, 1800]
saturation_s = 1800
"""


def test_spec028_r16_predictor_adapter_loads_and_is_allowed_in_production(
    tmp_path: Path,
) -> None:
    """[SPEC-028 R-16] predictor는 외부 모델이 아니라 mock 허용 없이 운영 프로필에서도 쓴다."""
    profile = _load(tmp_path, VALID + PREDICTOR)

    settings = profile.adapters["rules"].predictor
    assert settings is not None
    assert (settings.horizons_sec, settings.saturation_s) == ((600, 1800), 1800)
    check_profile(profile, allow_mock=False, has_openai_key=False)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("horizons_sec = [600, 1800]", "horizons_sec = []"),
        ("horizons_sec = [600, 1800]", "horizons_sec = [30]"),
        ("saturation_s = 1800", "saturation_s = 0"),
        ("[adapters.rules.predictor]\nhorizons_sec = [600, 1800]\nsaturation_s = 1800", ""),
    ],
)
def test_spec028_r16_invalid_predictor_profile_fails(tmp_path: Path, old: str, new: str) -> None:
    with pytest.raises(ProfileError):
        _load(tmp_path, VALID + PREDICTOR.replace(old, new))


def test_spec028_r16_predictor_cannot_serve_parse_or_embed(tmp_path: Path) -> None:
    """[SPEC-028 R-16] predictor는 /v1/analyze taskType만 처리한다."""
    text = (VALID + PREDICTOR).replace(
        "[parse]\nmax_query_chars = 200",
        '[parse]\nmax_query_chars = 200\nadapter = "rules"\nmodel = "x"',
    )

    with pytest.raises(ProfileError, match="predictor"):
        _load(tmp_path, text)


@pytest.mark.parametrize("name", ["local.toml", "local-mock.toml"])
def test_spec028_r16_committed_profiles_use_rule_predictor(name: str) -> None:
    path = Path(__file__).resolve().parents[1] / "profiles" / name
    profile = load_profile(path, GatewayProfile)

    task = profile.tasks["risk_forecast"]
    assert task.adapter is not None
    assert profile.adapters[task.adapter].kind == "predictor"
    assert profile.tasks["state_compare"].min_images == 2
