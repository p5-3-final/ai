"""외부 모델 없이 코드로 답하는 predictor Adapter(SPEC-028 R-16).

`risk_forecast`의 1단계는 규칙 점수다. 프롬프트·이미지가 없어 `VlmAdapter`와 따로 둔다. 학습 모델이
생기면 같은 Protocol의 구현과 프로필만 더한다.
"""

import math
from typing import Protocol

from ai_gateway.profile import PredictorSettings

type JsonObject = dict[str, object]

# 확률은 소수 셋째 자리에서 끊어 같은 입력이면 직렬화가 늘 같게 한다.
_DIGITS = 3


class Predictor(Protocol):
    """예측기 하나. 출력은 taskType 출력 스키마로 검증한다."""

    def predict(self, *, model: str, variables: JsonObject) -> JsonObject: ...


class RuleForecastPredictor:
    """활성 시간과 구역 분당 진입 수만으로 정해지는 노출 확률(SPEC-028 R-16, plan 4절 기본안).

    분당 진입 수를 초당 도착률로 바꾸고 활성 시간이 포화 시간보다 짧으면 비례해 줄인다
    (λ = 분당 진입 / 60 * min(1, 활성 / 포화)). 구간 동안 한 번 이상 진입할 확률은 포아송 도착으로
    p = 1 - exp(-λ * 구간)이다. 공식은 AI 담당자가 바꿀 수 있는 자리값이다.
    """

    def __init__(self, settings: PredictorSettings) -> None:
        self._settings = settings

    def predict(self, *, model: str, variables: JsonObject) -> JsonObject:
        active = _number(variables, "activeSeconds")
        entries = _number(variables, "zoneEntriesPerMinute")
        rate = entries / 60 * min(1.0, active / self._settings.saturation_s)
        return {
            "method": "rule",
            "horizons": [
                {
                    "horizonSec": horizon,
                    "exposureProbability": round(1 - math.exp(-rate * horizon), _DIGITS),
                }
                for horizon in self._settings.horizons_sec
            ],
            "features": {"active_seconds": active, "zone_entries_per_minute": entries},
        }


def _number(variables: JsonObject, name: str) -> float:
    # 입력 스키마 검증을 통과한 값이라 숫자다. bool은 int의 하위형이라 따로 막는다.
    value = variables.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number")
    return float(value)
