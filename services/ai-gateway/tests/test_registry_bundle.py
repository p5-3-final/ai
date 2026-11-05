import hashlib
import tarfile
from pathlib import Path

import pytest
from ai_gateway.prompts import PromptRegistry
from ai_gateway.registry_bundle import build_bundle, open_bundle, resolve_sources
from ai_gateway.settings import GatewaySettings
from vlm_common.profile import ProfileError
from vlm_storage.artifacts import ArtifactError
from vlm_storage.memory import InMemoryStorage

PROFILES = Path(__file__).resolve().parents[1] / "profiles"
KEY = "registry/test/registry.tar.gz"


@pytest.fixture
def bundle(registry_dir: Path, tmp_path: Path) -> tuple[bytes, str]:
    out = tmp_path / "registry.tar.gz"
    sha256 = build_bundle(registry_dir, PROFILES, out)
    return out.read_bytes(), sha256


def _storage(data: bytes) -> InMemoryStorage:
    s = InMemoryStorage()
    s.put_bytes(KEY, data, "application/gzip")
    return s


def _settings(**values: object) -> GatewaySettings:
    base: dict[str, object] = {
        "database_url": "postgresql+asyncpg://x",
        "profile_path": "/app/profiles/local-mock.toml",
        "KAFKA_BOOTSTRAP": "kafka:9092",
    }
    return GatewaySettings.model_validate(base | values)


def test_spec009_r26_bundle_has_prompts_taxonomy_and_profiles(
    bundle: tuple[bytes, str],
) -> None:
    """[SPEC-009 R-26] 묶음에는 프롬프트·분류 체계·프로필이 들어 있고 해시는 파일 해시다."""
    data, sha256 = bundle
    assert hashlib.sha256(data).hexdigest() == sha256
    with tarfile.open(fileobj=__import__("io").BytesIO(data)) as tar:
        names = tar.getnames()

    assert "prompts/event_judge/fallback.json" in names
    assert "taxonomy/v2/taxonomy.schema.json" in names
    assert "profiles/local-mock.toml" in names


def test_spec012_r23_bundle_has_query_dictionary(bundle: tuple[bytes, str]) -> None:
    """[SPEC-012 R-23] 질의 사전도 Registry 묶음에 들어간다."""
    data, _ = bundle
    with tarfile.open(fileobj=__import__("io").BytesIO(data)) as tar:
        names = tar.getnames()

    assert "query/dictionary.json" in names
    assert "query/dictionary.schema.json" in names


def test_spec009_r26_same_content_gives_same_bundle(
    registry_dir: Path, tmp_path: Path, bundle: tuple[bytes, str]
) -> None:
    """[SPEC-009 R-26] 같은 내용이면 언제 만들어도 같은 해시라 설정의 해시가 내용을 고정한다."""
    assert build_bundle(registry_dir, PROFILES, tmp_path / "again.tar.gz") == bundle[1]


def test_spec009_r26_opens_bundle_from_storage(bundle: tuple[bytes, str], tmp_path: Path) -> None:
    """[SPEC-009 R-26] 저장소의 묶음을 받아 풀고, 그 폴더로 Registry를 읽는다."""
    data, sha256 = bundle

    root = open_bundle(_storage(data), KEY, sha256, tmp_path)

    assert PromptRegistry.load(root).get("event_judge") is not None
    assert (root / "profiles/local-mock.toml").is_file()


def test_spec009_r27_bundle_hash_mismatch_fails(bundle: tuple[bytes, str], tmp_path: Path) -> None:
    """[SPEC-009 R-27] 묶음 해시가 설정과 다르면 실패한다."""
    with pytest.raises(ArtifactError, match="sha256"):
        open_bundle(_storage(bundle[0]), KEY, "0" * 64, tmp_path)


def test_spec009_r27_missing_bundle_fails(bundle: tuple[bytes, str], tmp_path: Path) -> None:
    """[SPEC-009 R-27] 저장소에 묶음이 없으면 실패한다."""
    with pytest.raises(ArtifactError, match="not found"):
        open_bundle(InMemoryStorage(), KEY, bundle[1], tmp_path)


def test_spec009_r28_reuses_opened_bundle(bundle: tuple[bytes, str], tmp_path: Path) -> None:
    """[SPEC-009 R-28] 해시가 같은 묶음을 이미 풀었으면 저장소를 읽지 않는다."""
    data, sha256 = bundle
    first = open_bundle(_storage(data), KEY, sha256, tmp_path)

    assert open_bundle(InMemoryStorage(), KEY, sha256, tmp_path) == first


def test_spec009_r26_without_bundle_settings_uses_image_paths(tmp_path: Path) -> None:
    """[SPEC-009 R-26] 묶음 설정이 없으면 이미지에 넣은 Registry와 프로필을 그대로 쓴다."""
    sources = resolve_sources(_settings(registry_dir=str(tmp_path)), None)

    assert sources.registry_dir == tmp_path
    assert sources.profile_path == Path("/app/profiles/local-mock.toml")


def test_spec009_r26_bundle_settings_read_profile_inside_bundle(
    bundle: tuple[bytes, str], tmp_path: Path
) -> None:
    """[SPEC-009 R-26] 묶음 설정이 있으면 프로필 경로는 묶음 안의 상대 경로다."""
    data, sha256 = bundle
    settings = _settings(
        registry_bundle=KEY,
        registry_bundle_sha256=sha256,
        profile_path="profiles/local-mock.toml",
        model_cache_dir=str(tmp_path),
    )

    sources = resolve_sources(settings, _storage(data))

    assert sources.profile_path == sources.registry_dir / "profiles/local-mock.toml"
    assert sources.profile_path.is_file()


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"registry_bundle": KEY}, "sha256"),
        ({"registry_bundle": KEY, "registry_bundle_sha256": "0" * 64}, "relative"),
    ],
)
def test_spec009_r14_invalid_bundle_settings_fail(
    values: dict[str, object], message: str, tmp_path: Path
) -> None:
    """[SPEC-009 R-14] 묶음 키·해시 중 하나만 있거나, 묶음인데 프로필이 절대 경로면 실패한다."""
    with pytest.raises(ProfileError, match=message):
        resolve_sources(_settings(model_cache_dir=str(tmp_path), **values), InMemoryStorage())


def test_spec009_r14_bundle_needs_models_bucket(tmp_path: Path) -> None:
    """[SPEC-009 R-14] 묶음을 받을 모델 버킷 설정이 없으면 실패한다."""
    settings = _settings(
        registry_bundle=KEY,
        registry_bundle_sha256="0" * 64,
        profile_path="profiles/local-mock.toml",
    )

    with pytest.raises(ProfileError, match="bucket"):
        resolve_sources(settings, None)


def test_spec009_r26_empty_bundle_settings_mean_no_bundle(tmp_path: Path) -> None:
    """[SPEC-009 R-26] compose의 빈 값(`${VAR:-}`)은 묶음 설정이 없는 것과 같다."""
    settings = _settings(registry_dir=str(tmp_path), registry_bundle="", registry_bundle_sha256="")

    assert resolve_sources(settings, None).registry_dir == tmp_path
