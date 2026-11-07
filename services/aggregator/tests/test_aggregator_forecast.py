"""영상 끝 활성 위험의 노출 예측(SPEC-028 R-14, R-15)."""

from agg_support import (
    AISLE,
    HAZARD,
    TRACEPARENT,
    FakeGateway,
    inputs,
    person,
    profile,
    risk_event,
    stay,
)
from aggregator.compute import Computed, compute
from aggregator.forecast import forecast_risks, zone_entries_per_minute
from aggregator.model import AggregateInput
from vlm_ai_client.client import AiGatewayError
from vlm_contracts.aggregates import GLOBAL_ZONE


def _scenario() -> tuple[AggregateInput, Computed]:
    source = inputs(
        risk_event(1, 30_000, detected=True),
        risk_event(2, 40_000, detected=True, risk_type="fall", zone=None),
        risk_event(3, 50_000, detected=True, risk_type="ppe_x", zone=AISLE),
        risk_event(4, 60_000, detected=False, risk_type="ppe_x", zone=AISLE),
        tracks=(person(10, 0, 600_000), person(11, 0, 600_000), person(12, 0, 10, "forklift")),
        presences=(
            stay(10, HAZARD, 100_000, 110_000),
            stay(10, HAZARD, 200_000, 210_000),
            stay(11, HAZARD, 300_000, 310_000),
            stay(12, HAZARD, 300_000, 310_000),
        ),
    )
    return source, compute(source, profile())


def test_spec028_r14_zone_entries_per_minute_counts_people_only() -> None:
    """[SPEC-028 R-14] 구역은 사람 트랙 진입 수 ÷ 영상 분, global은 사람 트랙 수 ÷ 영상 분이다."""
    source, _ = _scenario()

    assert zone_entries_per_minute(source, str(HAZARD), profile().profile) == 0.3
    assert zone_entries_per_minute(source, GLOBAL_ZONE, profile().profile) == 0.2
    assert zone_entries_per_minute(source, str(AISLE), profile().profile) == 0.0


async def test_spec028_r14_each_active_risk_at_end_is_forecast_with_variables() -> None:
    """[SPEC-028 R-14] 영상 끝 활성 위험마다 부른다.

    변수는 위험 유형·구역 종류·활성 시간·노출 지표·구역 분당 진입 수다.
    """
    source, computed = _scenario()
    gateway = FakeGateway()

    forecasts = await forecast_risks(
        gateway, source, computed, profile().profile, traceparent=TRACEPARENT
    )

    active = [r for r in computed.risks if r.status == "active"]
    assert [f.risk_id for f in forecasts] == [r.risk_id for r in active]
    assert gateway.task_types() == ["risk_forecast", "risk_forecast"]
    hazard = next(r for r, _ in gateway.requests if r.variables["zoneKind"] == "hazard")
    risk = next(r for r in active if r.zone_id == str(HAZARD))
    assert hazard.images == []
    assert hazard.variables == {
        "riskType": "ppe_missing",
        "zoneKind": "hazard",
        "activeSeconds": risk.exposure.active_seconds,
        "exposureCount": risk.exposure.exposure_count,
        "exposureSeconds": risk.exposure.exposure_seconds,
        "zoneEntriesPerMinute": 0.3,
    }
    assert [r.variables["zoneKind"] for r, _ in gateway.requests].count(None) == 1
    assert all(f.status == "generated" for f in forecasts)
    assert forecasts[0].forecast is not None
    assert forecasts[0].call_id is not None


async def test_spec028_r15_unconfigured_model_is_not_configured() -> None:
    """[SPEC-028 R-15] 미설정 결과(modelId null)면 not_configured이고 예측 값은 null이다."""
    source, computed = _scenario()
    gateway = FakeGateway(task_model_ids={"risk_forecast": None})
    gateway.task_outputs["risk_forecast"] = {"method": "rule", "horizons": [], "features": {}}

    forecasts = await forecast_risks(gateway, source, computed, profile().profile, traceparent=None)

    assert {(f.status, f.forecast) for f in forecasts} == {("not_configured", None)}


async def test_spec028_r15_error_or_invalid_output_is_failed() -> None:
    """[SPEC-028 R-15] 호출 오류·출력 형식 오류면 failed다."""
    source, computed = _scenario()
    broken = FakeGateway()
    broken.task_errors["risk_forecast"] = AiGatewayError(
        "AI_UNAVAILABLE", "x", status=503, retryable=True
    )
    invalid = FakeGateway()
    invalid.task_outputs["risk_forecast"] = {"method": "guess"}

    for gateway in (broken, invalid):
        forecasts = await forecast_risks(
            gateway, source, computed, profile().profile, traceparent=None
        )
        assert {(f.status, f.forecast) for f in forecasts} == {("failed", None)}


async def test_spec028_r14_no_active_risk_makes_no_call() -> None:
    gateway = FakeGateway()
    source = inputs()

    forecasts = await forecast_risks(
        gateway, source, compute(source, profile()), profile().profile, traceparent=None
    )

    assert (forecasts, gateway.requests) == ([], [])
