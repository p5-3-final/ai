"""기동 조립: 설정·프로필 검증, 워커 토픽, 주기 확인(SPEC-022 R-22)."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from stitcher_support import PROFILE
from track_stitcher.__main__ import build, load_stitch_profile
from track_stitcher.settings import TrackStitcherSettings, WorkerSettings
from track_stitcher.state import MemoryJobStateStore
from vlm_common.health import HealthState
from vlm_common.profile import ProfileError
from vlm_storage.memory import InMemoryStorage

LOCAL = Path(__file__).parents[1] / "profiles" / "local.toml"


def _settings(**values: object) -> TrackStitcherSettings:
    return TrackStitcherSettings.model_validate(
        {"profile_path": LOCAL, "redis_url": "redis://redis:6379/0"} | values
    )


def test_spec022_r22_missing_profile_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_stitch_profile(_settings(profile_path=tmp_path / "none.toml"))


def test_spec022_r22_local_profile_loads_for_startup() -> None:
    assert load_stitch_profile(_settings()).version == "local-1"


def test_redis_url_and_profile_are_required() -> None:
    with pytest.raises(ValidationError, match="redis_url"):
        TrackStitcherSettings(profile_path=LOCAL)
    with pytest.raises(ValidationError, match="profile_path"):
        TrackStitcherSettings(redis_url="redis://r")


@pytest.mark.parametrize("values", [{"sweep_interval_s": 0}, {"finalize_lease_s": 0}])
def test_invalid_intervals_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_redis_url_is_secret() -> None:
    assert "redis://redis" not in repr(_settings())


def test_worker_subscribes_three_topics_with_retry_and_dlq() -> None:
    worker, sweeper = build(
        WorkerSettings(kafka_bootstrap="localhost:1"),
        _settings(),
        InMemoryStorage(),
        MemoryJobStateStore(),
        PROFILE,
        HealthState(),
    )

    assert set(worker.required_topics()) == {
        "chunk.boundary",
        "segment.embedded",
        "video.deleted",
        "chunk.boundary.track-stitcher.retry",
        "segment.embedded.track-stitcher.retry",
        "video.deleted.track-stitcher.retry",
        "track-stitcher.dlq",
    }
    assert sweeper.sweep_once() == 0
