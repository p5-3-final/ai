"""실행당 VLM 호출 상한(SPEC-023 R-19)."""

import pytest
from event_analyzer.cap import call_cap
from event_analyzer.profile import CapProfile
from pydantic import ValidationError
from vlm_contracts.events import TimeRange


@pytest.mark.parametrize(
    ("owned_ms", "cap"),
    [
        (120_000, 6),  # 업로드 2분 청크
        (20_000, 3),  # 실시간 20초 창은 바닥 3
        (70_000, 4),  # 분당 3회, 70초면 3.5회라 올린다
        (1, 3),
    ],
)
def test_spec023_r19_cap_is_floor_or_per_minute_times_length(owned_ms: int, cap: int) -> None:
    assert call_cap(TimeRange(start_ms=0, end_ms=owned_ms), CapProfile()) == cap


def test_spec023_r19_cap_uses_profile_values() -> None:
    profile = CapProfile(floor=1, per_minute=10)

    assert call_cap(TimeRange(start_ms=60_000, end_ms=90_000), profile) == 5


@pytest.mark.parametrize("values", [{"floor": 0}, {"per_minute": 0}])
def test_spec023_r19_non_positive_cap_values_fail(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        CapProfile.model_validate(values)
