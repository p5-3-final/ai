"""grounding-worker 기동 조립: 설정 검증, 프로필·탐지기 로드, 워커 토픽."""

from pathlib import Path

import pytest
from grounding_worker.__main__ import build_worker, prepare
from grounding_worker.detector import MockDetector
from grounding_worker.settings import GroundingWorkerSettings, WorkerSettings
from pydantic import ValidationError
from vlm_common.health import HealthState
from vlm_common.profile import ProfileError
from vlm_storage.memory import InMemoryStorage

_MOCK = Path(__file__).resolve().parents[1] / "profiles" / "local-mock.toml"


def _settings(**values: object) -> GroundingWorkerSettings:
    return GroundingWorkerSettings.model_validate({"profile_path": _MOCK} | values)


def test_profile_path_is_required() -> None:
    with pytest.raises(ValidationError, match="profile_path"):
        GroundingWorkerSettings()


def test_spec026_r2_r13_defaults_follow_the_spec() -> None:
    settings = _settings()

    assert (settings.sample_interval_ms, settings.max_samples, settings.item_timeout_s) == (
        1_000,
        12,
        120,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"sample_interval_ms": 199},
        {"max_samples": 13},
        {"max_samples": 0},
        {"item_timeout_s": 0},
        {"work_dir": "relative/dir"},
        {"work_dir": "/work/a:b"},
    ],
)
def test_invalid_settings_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_mock_profile_without_allow_mock_fails_startup() -> None:
    with pytest.raises(ProfileError, match="ALLOW_MOCK"):
        prepare(_settings(), None)


def test_spec026_r3_r17_prepare_loads_detector_and_threshold_once() -> None:
    prepared = prepare(_settings(allow_mock=True, sample_interval_ms=500), None)

    assert isinstance(prepared.detector, MockDetector)
    assert prepared.detector.version == "mock-grounding-v1"
    assert prepared.config.score_threshold == 0.25
    assert prepared.config.sample_interval_ms == 500


def test_spec026_r14_r15_worker_uses_retry_and_dlq_topics() -> None:
    settings = _settings(allow_mock=True)
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        settings,
        InMemoryStorage(),
        prepare(settings, None),
        HealthState(),
    )

    assert worker.required_topics() == [
        "grounding.requested",
        "grounding.requested.grounding-worker.retry",
        "grounding-worker.dlq",
    ]


_OWL = _MOCK.with_name("local.toml")


def test_models_bucket_settings_default_and_blank() -> None:
    settings = _settings(models_bucket="")

    assert settings.models_bucket is None
    assert settings.model_cache_dir == Path("/var/cache/vlm-models")


def test_owl_profile_without_models_bucket_fails_startup() -> None:
    with pytest.raises(ProfileError, match="MODELS_BUCKET"):
        prepare(_settings(profile_path=_OWL), None)
