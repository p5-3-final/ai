"""aggregator 프로필(SPEC-027 R-21)."""

import re
from pathlib import Path

import pytest
from aggregator.profile import DEFAULT_PROFILE_PATH, load_aggregate_profile
from vlm_common.profile import ProfileError


def test_spec027_r21_default_profile_loads_with_versioned_name() -> None:
    """[SPEC-027 R-21] 버전은 `<name>@<내용 sha256 앞 12자리>`라 기준값이 바뀌면 바뀐다."""
    loaded = load_aggregate_profile(DEFAULT_PROFILE_PATH)

    assert re.fullmatch(r"aggregate-v1@[0-9a-f]{12}", loaded.version)
    assert (loaded.profile.merge_gap_ms, loaded.profile.dwell_ms) == (10_000, 60_000)
    assert (loaded.profile.loiter_window_ms, loaded.profile.loiter_count) == (300_000, 3)
    assert loaded.profile.person_classes == ("person",)


def test_spec027_r21_changed_threshold_changes_version(tmp_path: Path) -> None:
    changed = tmp_path / "p.toml"
    changed.write_text(
        DEFAULT_PROFILE_PATH.read_text(encoding="utf-8").replace(
            "dwell_ms = 60000", "dwell_ms = 90000"
        ),
        encoding="utf-8",
    )

    assert (
        load_aggregate_profile(changed).version
        != load_aggregate_profile(DEFAULT_PROFILE_PATH).version
    )


def test_spec027_r21_unknown_key_fails(tmp_path: Path) -> None:
    broken = tmp_path / "p.toml"
    broken.write_text('name = "x"\nmerge_gap = 1\n', encoding="utf-8")

    with pytest.raises(ProfileError):
        load_aggregate_profile(broken)
