"""grounding-worker 프로필 검증과 탐지기 선택(SPEC-026 R-3, R-17)."""

from pathlib import Path

import pytest
from grounding_worker.detector import MockDetector, load_detector
from grounding_worker.profile import GroundingProfile, OwlDetectorProfile
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


_OWL = """
[detector]
kind = "owl"
manifest = "models/owlvit/owlvit-b32/manifest.json"
manifest_sha256 = "d61e583174e35fbce8264447cbb8e2864afb97f13489b491b718ba3bf61f1cc2"
score_threshold = 0.1
"""


def test_spec026_r17_local_owl_profile_loads() -> None:
    profile = load_profile(_PROFILES / "local.toml", GroundingProfile)

    assert isinstance(profile.detector, OwlDetectorProfile)
    assert profile.detector.manifest == "models/owlvit/owlvit-b32/manifest.json"
    assert 0 <= profile.detector.score_threshold <= 1


def test_owl_profile_defaults_nms_iou(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, _OWL), GroundingProfile)

    assert isinstance(profile.detector, OwlDetectorProfile)
    assert profile.detector.nms_iou == 0.3


@pytest.mark.parametrize(
    "replace",
    [
        ('manifest = "models/owlvit/owlvit-b32/manifest.json"\n', ""),
        ('manifest_sha256 = "d61e', 'manifest_sha256 = "D61E'),
        ('manifest = "models/', 'manifest = "../'),
        ("score_threshold = 0.1", 'score_threshold = 0.1\nversion = "v1"'),
        ("score_threshold = 0.1", "score_threshold = 0.1\nnms_iou = 0"),
    ],
)
def test_spec026_r17_invalid_owl_profile_fails_startup(
    tmp_path: Path, replace: tuple[str, str]
) -> None:
    with pytest.raises(ProfileError):
        load_profile(_write(tmp_path, _OWL.replace(*replace)), GroundingProfile)


def test_owl_detector_requires_models_bucket(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, _OWL), GroundingProfile)

    with pytest.raises(ProfileError, match="MODELS_BUCKET"):
        load_detector(profile.detector, allow_mock=False, models=None, cache_dir=tmp_path)
