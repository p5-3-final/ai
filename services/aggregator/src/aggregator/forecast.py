"""영상 끝 활성 위험의 노출 예측(SPEC-028 R-14, R-15).

위험마다 ai-gateway `risk_forecast`를 한 번 부른다. 예측이 없거나 실패해도 나머지 결과는 그대로
낸다.
"""

import logging

from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest
from vlm_contracts.aggregates import GLOBAL_ZONE, Forecast, Risk, RiskForecast

from aggregator.compute import Computed
from aggregator.model import AggregateInput
from aggregator.profile import AggregateProfile
from aggregator.summary import AnalyzeClient

FORECAST_TASK = "risk_forecast"
# 분당 진입 수는 소수 셋째 자리에서 끊어 같은 입력이면 요청이 늘 같게 한다(R-17).
_DIGITS = 3

logger = logging.getLogger(__name__)


def zone_entries_per_minute(
    source: AggregateInput, zone_id: str, profile: AggregateProfile
) -> float:
    """구역은 사람 트랙 진입 수 ÷ 영상 분, global은 사람 트랙 수 ÷ 영상 분이다(plan 4절)."""
    people = {t.track_id for t in source.tracks if t.class_name in profile.person_classes}
    if zone_id == GLOBAL_ZONE:
        count = len(people)
    else:
        count = sum(
            1 for p in source.presences if p.track_id in people and str(p.zone_id) == zone_id
        )
    return round(count / (source.duration_ms / 60_000), _DIGITS)


def active_at_end(computed: Computed) -> list[Risk]:
    """영상 끝에 활성인 위험(이어 온 위험 포함). 순서는 결과 파일 risks 순서다."""
    return [r for r in computed.risks if r.status == "active"]


async def forecast_risks(
    client: AnalyzeClient,
    source: AggregateInput,
    computed: Computed,
    profile: AggregateProfile,
    *,
    traceparent: str | None,
) -> list[Forecast]:
    """영상 끝 활성 위험마다 예측한다.

    | 응답 | 상태 |
    | --- | --- |
    | `modelId` null(미설정 대체 결과) | `not_configured`, 예측 null |
    | 호출 오류(SPEC-021 R-9 재시도 뒤)·출력 형식 오류 | `failed`, 예측 null |
    | 그 밖 | `generated` |
    """
    kinds = {str(z.zone_id): z.kind for z in source.zones}
    forecasts: list[Forecast] = []
    for risk in active_at_end(computed):
        request = AnalyzeRequest(
            task_type=FORECAST_TASK,
            source_id=source.source_id,
            images=[],
            variables={
                "riskType": risk.risk_type,
                "zoneKind": kinds.get(risk.zone_id),
                "activeSeconds": risk.exposure.active_seconds,
                "exposureCount": risk.exposure.exposure_count,
                "exposureSeconds": risk.exposure.exposure_seconds,
                "zoneEntriesPerMinute": zone_entries_per_minute(source, risk.zone_id, profile),
            },
        )
        forecasts.append(await _one(client, request, risk, traceparent))
    return forecasts


async def _one(
    client: AnalyzeClient, request: AnalyzeRequest, risk: Risk, traceparent: str | None
) -> Forecast:
    try:
        response = await client.analyze(request, traceparent=traceparent)
    except AiGatewayError as e:
        logger.warning("risk forecast failed", extra={"riskId": str(risk.risk_id), "code": e.code})
        return Forecast(risk_id=risk.risk_id, status="failed", forecast=None, call_id=None)
    if response.model_id is None:
        return Forecast(
            risk_id=risk.risk_id, status="not_configured", forecast=None, call_id=response.call_id
        )
    try:
        forecast = RiskForecast.model_validate(response.output)
    except ValidationError:
        logger.warning("risk forecast output invalid", extra={"riskId": str(risk.risk_id)})
        return Forecast(
            risk_id=risk.risk_id, status="failed", forecast=None, call_id=response.call_id
        )
    return Forecast(
        risk_id=risk.risk_id, status="generated", forecast=forecast, call_id=response.call_id
    )
