"""영상 요약(SPEC-027 R-18, R-19)."""

from agg_support import (
    HAZARD,
    TRACEPARENT,
    FakeGateway,
    analysis,
    event,
    inputs,
    profile,
    risk_event,
    uid,
)
from aggregator.compute import compute
from aggregator.summary import summarize
from vlm_ai_client.client import AiGatewayError


async def test_spec027_r18_calls_source_summary_once_without_images() -> None:
    """[SPEC-027 R-18] 확정 이벤트가 있으면 이미지 없이 한 번 부른다.

    변수는 타임라인·위험·구역·영상 길이다.
    """
    source = inputs(risk_event(1, 30_000, detected=True))
    computed = compute(source, profile())
    gateway = FakeGateway()

    summary = await summarize(gateway, source, computed, traceparent=TRACEPARENT)

    [(request, trace)] = gateway.requests
    assert (request.task_type, request.images, trace) == ("source_summary", [], TRACEPARENT)
    variables = request.variables
    assert set(variables) == {"durationSec", "zones", "timeline", "risks"}
    assert variables["durationSec"] == 600.0
    assert {"zoneId": str(HAZARD), "name": "A동 크레인 하부", "kind": "hazard"} in variables[
        "zones"
    ]  # type: ignore[operator]
    assert "description" not in str(variables)
    assert (summary.status, summary.text, summary.call_id) == (
        "generated",
        "요약 문장 원문",
        uid(0xCA12),
    )
    assert summary.prompt_version == "source_summary@0123456789ab"


async def test_spec027_r18_nothing_confirmed_is_empty_without_call() -> None:
    """[SPEC-027 R-18] 확정 이벤트와 위험이 없으면 부르지 않고 empty다."""
    source = inputs(event(1, start=0, result=analysis(status="rejected")))
    gateway = FakeGateway()

    summary = await summarize(gateway, source, compute(source, profile()), traceparent=None)

    assert (summary.status, gateway.requests) == ("empty", [])


async def test_spec027_r19_unconfigured_model_is_not_configured() -> None:
    """[SPEC-027 R-19] modelId null이면 not_configured다."""
    source = inputs(risk_event(1, 30_000, detected=True))
    gateway = FakeGateway(model_id=None, output={"summary": None, "highlights": []})

    summary = await summarize(gateway, source, compute(source, profile()), traceparent=None)

    assert (summary.status, summary.text, summary.model_id) == ("not_configured", None, None)


async def test_spec027_r19_error_or_fallback_output_is_failed() -> None:
    """[SPEC-027 R-19] 호출 오류, 또는 모델이 있는데 summary가 null(대체 결과)이면 failed다."""
    source = inputs(risk_event(1, 30_000, detected=True))
    computed = compute(source, profile())
    error = FakeGateway(error=AiGatewayError("AI_UNAVAILABLE", "x", status=503, retryable=True))
    fallback = FakeGateway(output={"summary": None, "highlights": []})
    malformed = FakeGateway(output={"summary": 3})

    for gateway in (error, fallback, malformed):
        summary = await summarize(gateway, source, computed, traceparent=None)
        assert (summary.status, summary.text, summary.highlights) == ("failed", None, [])
