from pathlib import Path

import pytest
from vlm_common.errors import VlmError
from vlm_common.profile import Profile, ProfileError, load_profile


class EncoderProfile(Profile):
    model_id: str
    dimension: int


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_profile_returns_validated_model(tmp_path: Path) -> None:
    path = _write(
        tmp_path, 'model_id = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"\ndimension = 512\n'
    )

    profile = load_profile(path, EncoderProfile)

    assert profile == EncoderProfile(
        model_id="open_clip:MobileCLIP2-S0:dfndr2b:mean-4", dimension=512
    )


def test_missing_file_raises_profile_error_with_path(tmp_path: Path) -> None:
    path = tmp_path / "absent.toml"

    with pytest.raises(ProfileError, match=r"absent\.toml"):
        load_profile(path, EncoderProfile)


def test_invalid_toml_raises_profile_error(tmp_path: Path) -> None:
    path = _write(tmp_path, "model_id = \n")

    with pytest.raises(ProfileError, match="TOML"):
        load_profile(path, EncoderProfile)


def test_schema_violation_names_the_field(tmp_path: Path) -> None:
    path = _write(tmp_path, 'model_id = "x"\ndimension = "wide"\n')

    with pytest.raises(ProfileError, match="dimension"):
        load_profile(path, EncoderProfile)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    # 오타 난 키를 조용히 무시하면 기본값으로 돌아 다른 모델이 쓰일 수 있다.
    path = _write(tmp_path, 'model_id = "x"\ndimension = 512\ndimnesion = 768\n')

    with pytest.raises(ProfileError, match="dimnesion"):
        load_profile(path, EncoderProfile)


def test_profile_is_immutable(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, 'model_id = "x"\ndimension = 512\n'), EncoderProfile)

    with pytest.raises(ValueError, match="frozen"):
        profile.dimension = 768  # type: ignore[misc]  # 불변 검증용 대입 (#8)


def test_profile_error_is_startup_failure_code() -> None:
    error = ProfileError("bad")

    assert isinstance(error, VlmError)
    assert error.code == "PROFILE_INVALID"
