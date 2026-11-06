from pathlib import Path

import pytest
from frame_encoder.profile import FrameEncoderProfile
from vlm_common.profile import ProfileError, load_profile

ENCODER = """
[encoder]
kind = "mock"
model_id = "mock:mock:mock:mean4"
dimension = 8
"""


def _load(tmp_path: Path, body: str) -> FrameEncoderProfile:
    path = tmp_path / "profile.toml"
    path.write_text(ENCODER + body, encoding="utf-8")
    return load_profile(path, FrameEncoderProfile)


def test_profile_without_prototypes_is_valid(tmp_path: Path) -> None:
    profile = _load(tmp_path, "")

    assert profile.prototypes is None
    assert profile.encoding.batch_size == 16


def test_profile_reads_prototype_types(tmp_path: Path) -> None:
    profile = _load(
        tmp_path,
        """
[prototypes]
version = "local-sample-v1"

[prototypes.types.fall]
texts = ["a worker lying on the floor", "a person fallen down"]
threshold = 0.25
""",
    )

    assert profile.prototypes is not None
    fall = profile.prototypes.types["fall"]
    assert (fall.texts, fall.threshold) == (
        ["a worker lying on the floor", "a person fallen down"],
        0.25,
    )


@pytest.mark.parametrize(
    "type_body",
    [
        # 분류 체계 v2의 탐지 가능 유형이 아니다
        '[prototypes.types.other]\ntexts = ["x"]\nthreshold = 0.2',
        '[prototypes.types.unknown_type]\ntexts = ["x"]\nthreshold = 0.2',
        # 문장이 없다
        "[prototypes.types.fall]\ntexts = []\nthreshold = 0.2",
        '[prototypes.types.fall]\ntexts = ["  "]\nthreshold = 0.2',
        # 임계값이 -1 초과 1 이하가 아니다
        '[prototypes.types.fall]\ntexts = ["x"]\nthreshold = -1.0',
        '[prototypes.types.fall]\ntexts = ["x"]\nthreshold = 1.01',
    ],
)
def test_spec_019_r15_invalid_prototype_type_fails_startup(tmp_path: Path, type_body: str) -> None:
    with pytest.raises(ProfileError):
        _load(tmp_path, f'[prototypes]\nversion = "v1"\n\n{type_body}\n')


def test_spec_019_r15_prototypes_without_types_fail_startup(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        _load(tmp_path, '[prototypes]\nversion = "v1"\ntypes = {}\n')


def test_threshold_of_one_is_allowed(tmp_path: Path) -> None:
    profile = _load(
        tmp_path,
        '[prototypes]\nversion = "v1"\n[prototypes.types.fall]\ntexts = ["x"]\nthreshold = 1.0\n',
    )

    assert profile.prototypes is not None
    assert profile.prototypes.types["fall"].threshold == 1.0


@pytest.mark.parametrize("name", ["local.toml", "local-mock.toml"])
def test_shipped_profiles_are_valid(name: str) -> None:
    # 이미지에 넣는 프로필이 틀리면 기동에 실패하므로 미리 확인한다.
    profiles = Path(__file__).resolve().parents[1] / "profiles"

    profile = load_profile(profiles / name, FrameEncoderProfile)

    assert profile.prototypes is not None
