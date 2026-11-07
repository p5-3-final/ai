"""risk_forecast 규칙 점수 predictor(SPEC-028 R-16)."""

import math

import pytest
from ai_gateway.adapters.predictor import RuleForecastPredictor
from ai_gateway.profile import PredictorSettings

VARIABLES: dict[str, object] = {
    "riskType": "barrier_not_restored",
    "zoneKind": "aisle",
    "activeSeconds": 1200.0,
    "exposureCount": 3,
    "exposureSeconds": 95.5,
    "zoneEntriesPerMinute": 0.8,
}


def _predictor() -> RuleForecastPredictor:
    return RuleForecastPredictor(PredictorSettings(horizons_sec=(600, 1800), saturation_s=1800))


def test_spec028_r16_rule_score_follows_the_profile_formula() -> None:
    """[SPEC-028 R-16] λ = 분당 진입/60 * min(1, 활성/포화), p = 1 - exp(-λ * 구간)."""
    output = _predictor().predict(model="rule-forecast-v1", variables=VARIABLES)

    rate = 0.8 / 60 * (1200 / 1800)
    assert output == {
        "method": "rule",
        "horizons": [
            {"horizonSec": 600, "exposureProbability": round(1 - math.exp(-rate * 600), 3)},
            {"horizonSec": 1800, "exposureProbability": round(1 - math.exp(-rate * 1800), 3)},
        ],
        "features": {"active_seconds": 1200.0, "zone_entries_per_minute": 0.8},
    }


def test_spec028_r16_only_active_time_and_entries_matter() -> None:
    """[SPEC-028 R-16] 활성 시간·분당 진입 수만으로 정해진다. 다른 변수는 점수를 바꾸지 않는다."""
    changed = {**VARIABLES, "riskType": "fall", "zoneKind": None, "exposureCount": 0}

    first = _predictor().predict(model="m", variables=VARIABLES)

    assert _predictor().predict(model="m", variables=changed) == first
    assert _predictor().predict(model="m", variables=VARIABLES) == first


@pytest.mark.parametrize(
    ("active", "entries"), [(0.0, 5.0), (10_000.0, 0.0), (10_000.0, 1000.0), (1.0, 0.01)]
)
def test_spec028_r16_probabilities_stay_between_zero_and_one(active: float, entries: float) -> None:
    variables = {**VARIABLES, "activeSeconds": active, "zoneEntriesPerMinute": entries}

    output = _predictor().predict(model="m", variables=variables)

    horizons = output["horizons"]
    assert isinstance(horizons, list)
    for h in horizons:
        assert 0 <= h["exposureProbability"] <= 1
