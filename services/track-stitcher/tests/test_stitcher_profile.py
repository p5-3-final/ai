"""기준값 프로필 검증(SPEC-022 R-22)."""

from pathlib import Path

import pytest
from track_stitcher.profile import StitchProfile
from vlm_common.profile import ProfileError, load_profile

LOCAL = Path(__file__).parents[1] / "profiles" / "local.toml"

VALID = """
version = "t-1"

[boundary]
min_mean_iou = 0.5
min_common_observations = 2
time_tolerance_ms = 100

[reid]
min_similarity = 0.8
max_gap_ms = 60000

[finalize]
wait_timeout_s = 1800
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_spec022_r22_local_profile_loads() -> None:
    profile = load_profile(LOCAL, StitchProfile)

    assert profile.version
    assert 0 <= profile.boundary.min_mean_iou <= 1
    assert profile.finalize.wait_timeout_s == 1800


def test_spec022_r22_missing_profile_fails(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "none.toml", StitchProfile)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("min_mean_iou = 0.5", "min_mean_iou = 1.5"),
        ("min_mean_iou = 0.5", "min_mean_iou = -0.1"),
        ("min_similarity = 0.8", "min_similarity = 1.01"),
        ("min_similarity = 0.8", "min_similarity = -0.5"),
        ("max_gap_ms = 60000", "max_gap_ms = 0"),
        ("wait_timeout_s = 1800", "wait_timeout_s = 0"),
        ("min_common_observations = 2", "min_common_observations = 0"),
        ("time_tolerance_ms = 100", "time_tolerance_ms = -1"),
        ('version = "t-1"', 'version = "bad version"'),
    ],
)
def test_spec022_r22_out_of_range_values_fail(tmp_path: Path, old: str, new: str) -> None:
    path = _write(tmp_path, VALID.replace(old, new))

    with pytest.raises(ProfileError):
        load_profile(path, StitchProfile)


def test_spec022_r22_unknown_key_fails(tmp_path: Path) -> None:
    path = _write(tmp_path, VALID + "\n[reid_extra]\nenabled = false\n")

    with pytest.raises(ProfileError):
        load_profile(path, StitchProfile)
