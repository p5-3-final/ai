"""실시간 긴급 후보 → alert.raised(SPEC-023 R-21~R-24)."""

from dataclasses import replace

import pytest
from ea_support import (
    CAMERA,
    DETECTOR_PROFILE,
    NOW,
    PROFILE_PATH,
    SOURCE,
    TRACEPARENT,
    FakeGateway,
    RecordingPublisher,
    alert_candidate,
    detected,
    detector_message,
    event_errors,
    gateway_error,
    judged,
)
from event_analyzer.alerts import AlertService
from event_analyzer.detector import detector_request
from event_analyzer.ids import alert_event_id
from event_analyzer.profile import EventAnalyzerProfile
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest
from vlm_common.profile import load_profile
from vlm_contracts.events import AlertCandidate, AlertRaised, VerificationStatus
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext

PROFILE = load_profile(PROFILE_PATH, EventAnalyzerProfile)
CTX = MessageContext(
    topic="alert.candidate", partition=0, offset=0, key=None, attempt=1, traceparent=TRACEPARENT
)


async def _run(
    message: AlertCandidate, gateway: FakeGateway | None = None, ctx: MessageContext = CTX
) -> tuple[FakeGateway, list[AlertRaised]]:
    fake = gateway or FakeGateway()
    publisher = RecordingPublisher()
    await AlertService(fake, publisher, PROFILE, clock=lambda: NOW).handle(message, ctx)
    alerts: list[AlertRaised] = []
    for topic, event, key, traceparent in publisher.published:
        assert (topic, key, traceparent) == ("alert.raised", str(SOURCE), TRACEPARENT)
        assert isinstance(event, AlertRaised)
        assert event_errors(event) == [], event
        alerts.append(event)
    return fake, alerts


async def test_spec023_r21_pending_then_confirmed() -> None:
    message = alert_candidate("fall")
    gateway = FakeGateway(reply=lambda r: judged("fall", severity="high"))

    _, [pending, confirmed] = await _run(message, gateway)

    assert pending.status == "pending"
    assert pending.event_id == alert_event_id(message.candidate_id, "pending")
    assert (pending.analysis, pending.dismiss_reason) == (None, None)
    assert pending.severity == PROFILE.rules["fall"].severity
    assert (pending.candidate_id, pending.candidate_event_type, pending.reason) == (
        message.candidate_id,
        "fall",
        "severity_high",
    )
    assert (pending.track_id, pending.zone_id, pending.observed_ms) == (
        message.track_id,
        None,
        message.observed_ms,
    )
    assert pending.evidence_frame_keys == message.evidence_frame_keys
    assert (pending.job_id, pending.camera_id) == (message.job_id, CAMERA)
    assert confirmed.status == "confirmed"
    assert confirmed.event_id == alert_event_id(message.candidate_id, "confirmed")
    assert confirmed.analysis is not None
    assert confirmed.analysis.model_dump(by_alias=True) == judged("fall", severity="high")
    assert confirmed.severity == "high"


async def test_spec023_r21_judges_once_without_escalation() -> None:
    gateway = FakeGateway(reply=lambda r: judged("fall", severity="high"))

    await _run(alert_candidate(), gateway)

    assert gateway.task_types() == ["event_judge"]


async def test_spec023_r21_confirmed_without_severity_uses_default() -> None:
    gateway = FakeGateway(reply=lambda r: {**judged("fall"), "severity": None})

    _, [_, confirmed] = await _run(alert_candidate("fall"), gateway)

    assert confirmed.severity == PROFILE.rules["fall"].severity


@pytest.mark.parametrize(
    ("status", "reason"),
    [("rejected", "rejected"), ("unverified", "unverified"), ("unknown", "unknown")],
)
async def test_spec023_r21_r23_not_confirmed_is_dismissed_with_reason(
    status: VerificationStatus, reason: str
) -> None:
    gateway = FakeGateway(reply=lambda r: judged("fall", status))

    _, [pending, dismissed] = await _run(alert_candidate(), gateway)

    assert pending.status == "pending"
    assert (dismissed.status, dismissed.dismiss_reason, dismissed.analysis) == (
        "dismissed",
        reason,
        None,
    )
    assert dismissed.event_id == alert_event_id(pending.candidate_id, "dismissed")


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_TIMEOUT", None, retryable=True),
        gateway_error("AI_DEADLINE_EXCEEDED", 504),
        gateway_error("AI_CALLER_FORBIDDEN", 403),
    ],
)
async def test_spec023_r23_failed_call_closes_pending_as_unknown(error: AiGatewayError) -> None:
    # pending이 이미 나갔으므로 메시지를 DLQ로 보내지 않고 알림을 닫는다.
    _, [_, dismissed] = await _run(alert_candidate(), FakeGateway(reply=lambda r: error))

    assert (dismissed.status, dismissed.dismiss_reason) == ("dismissed", "unknown")


async def test_spec023_r22_alert_request_matches_window_candidate_request() -> None:
    message = alert_candidate("fall", first_seen_ms=302_400, observed_ms=302_800, frames=2)
    gateway = FakeGateway()
    await _run(message, gateway)
    [(alert_request, _)] = gateway.requests
    window = detected(1, "fall", start_s=302.4, end_s=302.8, score=message.score).model_copy(
        update={
            "candidate_id": message.candidate_id,
            "observation_count": message.observations,
            "evidence_frame_keys": message.evidence_frame_keys,
        }
    )

    window_request = detector_request(window, detector_message(window))

    assert _without_trigger(alert_request) == _without_trigger(window_request)
    assert alert_request.variables["evidence"] == {
        "path": "detector",
        "score": message.score,
        "observationCount": message.observations,
        "detectorProfile": DETECTOR_PROFILE,
    }


def _without_trigger(request: AnalyzeRequest) -> dict[str, object]:
    # alert.candidate에는 triggerClass가 없다(계약 변경 필요로 보고). 나머지 입력은 같아야 한다.
    body = request.model_dump()
    evidence = dict(body["variables"]["evidence"])
    evidence.pop("triggerClass", None)
    body["variables"] = {**body["variables"], "evidence": evidence}
    return body


async def test_spec023_r24_records_alert_stage_until_pending() -> None:
    ctx = replace(CTX, timing=StageRecorder(service="event-analyzer", attempt=1, received_at=NOW))

    await _run(alert_candidate(), ctx=ctx)

    assert [(t.stage_id, t.count, t.unit) for t in ctx.timing.timings] == [
        ("analyzer.alert", 1, "candidates"),
        ("analyzer.judge", 1, "candidates"),
    ]
