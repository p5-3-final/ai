"""grounding-worker 프로필 검증과 탐지기 선택(SPEC-026 R-3, R-17)."""

from pathlib import Path

import pytest
from grounding_worker.detector import MockDetector, load_detector
from grounding_worker.profile import GroundingProfile
from vlm_common.profile import ProfileError, load_profile

_PROFILES = Path(__file__).resolve().parents[1] / "profiles"
_VALID = """
[detector]
kind = "mock"
version = "mock-grounding-v1"
score_threshold = 0.3
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_spec026_r17_local_mock_profile_loads() -> None:
    profile = load_profile(_PROFILES / "local-mock.toml", GroundingProfile)

    assert profile.detector.kind == "mock"
    assert profile.detector.version == "mock-grounding-v1"
    assert 0 <= profile.detector.score_threshold <= 1
    assert profile.render.font_path.is_absolute()


def test_render_defaults_apply_when_section_is_missing(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, _VALID), GroundingProfile)

    assert (profile.render.preset, profile.render.crf, profile.render.line_width) == (
        "veryfast",
        23,
        3,
    )


@pytest.mark.parametrize(
    "replace",
    [
        ('kind = "mock"', 'kind = "yoloe"'),
        ("score_threshold = 0.3", "score_threshold = 1.5"),
        ('version = "mock-grounding-v1"', 'version = "mock:v1/x"'),
        ("score_threshold = 0.3", "score_threshold = 0.3\nscore = 0.1"),
    ],
)
def test_spec026_r17_invalid_detector_profile_fails_startup(
    tmp_path: Path, replace: tuple[str, str]
) -> None:
    with pytest.raises(ProfileError):
        load_profile(_write(tmp_path, _VALID.replace(*replace)), GroundingProfile)


@pytest.mark.parametrize(
    "render",
    [
        'font_path = "relative/font.ttf"',
        'font_path = "/fonts/it\'s.ttf"',
        'font_path = "/fonts/a:b.ttf"',
        'preset = "fastest"',
        "crf = 60",
    ],
)
def test_invalid_render_profile_fails_startup(tmp_path: Path, render: str) -> None:
    with pytest.raises(ProfileError):
        load_profile(_write(tmp_path, f"{_VALID}\n[render]\n{render}\n"), GroundingProfile)


def test_mock_detector_requires_allow_mock(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, _VALID), GroundingProfile)

    with pytest.raises(ProfileError, match="ALLOW_MOCK"):
        load_detector(profile.detector, allow_mock=False)


def test_spec026_r17_mock_detector_version_comes_from_profile(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, _VALID), GroundingProfile)

    detector = load_detector(profile.detector, allow_mock=True)

    assert isinstance(detector, MockDetector)
    assert detector.version == "mock-grounding-v1"
