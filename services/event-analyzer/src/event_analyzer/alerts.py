"""실시간 긴급 후보(`alert.candidate`) → `alert.raised`(SPEC-023 R-21~R-24).

창이 끝날 때까지 기다리지 않고 탐지기 근거만으로 pending을 먼저 낸 뒤 `event_judge` 1회로 닫는다.
이벤트는 저장하지 않는다. 같은 candidateId의 창 후보가 같은 요청을 보내 ai-gateway 결과 캐시가
적중하고, 그쪽이 `event.analyzed`로 저장한다.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from vlm_ai_client.models import AnalyzeRequest
from vlm_contracts.events import (
    AlertCandidate,
    AlertRaised,
    AlertStatus,
    DismissReason,
    Severity,
    VLMAnalysis,
)
from vlm_messaging.worker import MessageContext, NonRetryableError

from event_analyzer import ids
from event_analyzer.calls import AnalyzeClient, EventPublisher, judge, judge_request, publish
from event_analyzer.judgment import Outcome
from event_analyzer.profile import EventAnalyzerProfile

logger = logging.getLogger(__name__)

# 알림 하나만 판정하므로 동시 호출은 1이다.
_ONE_CALL = 1


def alert_request(message: AlertCandidate) -> AnalyzeRequest:
    """긴급 후보의 판정 요청. 창 후보(`detector_request`)와 같은 모양이다(SPEC-023 R-22).

    시각은 최초 관측(시작)과 최소 관측 수를 넘긴 프레임(끝)이다. 계약에 트리거 클래스·구역 이름이
    없어 근거에서 빠진다.
    """
    return judge_request(
        source_id=message.source_id,
        event_type=message.candidate_event_type,
        start_ms=message.first_seen_ms,
        end_ms=message.observed_ms,
        zone_name=None,
        frame_keys=message.evidence_frame_keys,
        evidence={
            "path": "detector",
            "score": message.score,
            "observationCount": message.observations,
            "detectorProfile": message.detector_profile,
        },
    )


class AlertService:
    """`alert.candidate` 핸들러.

    pending을 낸 뒤의 판정 실패는 메시지를 DLQ로 보내지 않고 dismissed(unknown)로 닫는다. 알림이
    pending으로 남아 있는 편이 더 나쁘다. 발행 오류는 그대로 던져 다시 처리하고, 같은 eventId라
    소비자가 중복을 버린다.
    """

    def __init__(
        self,
        client: AnalyzeClient,
        publisher: EventPublisher,
        profile: EventAnalyzerProfile,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._publisher = publisher
        self._profile = profile
        self._clock = clock

    async def handle(self, message: AlertCandidate, ctx: MessageContext) -> None:
        """워커 경로 핸들러."""
        default = self._profile.rules[message.candidate_event_type].severity
        # R-24: 받은 뒤 첫 alert.raised(pending)를 낼 때까지가 알림 지연이다.
        with ctx.timing.stage("analyzer.alert", unit="candidates") as block:
            pending = self._raised(message, "pending", default)
            await asyncio.to_thread(self._publish, pending, ctx.traceparent)
            block.count = 1
        with ctx.timing.stage("analyzer.judge", unit="candidates") as block:
            outcome = await self._judge(message, ctx)
            block.count = 1
        await asyncio.to_thread(
            self._publish, self._closing(message, outcome, default), ctx.traceparent
        )

    async def _judge(self, message: AlertCandidate, ctx: MessageContext) -> Outcome:
        try:
            return await judge(
                self._client,
                alert_request(message),
                candidate_id=message.candidate_id,
                event_type=message.candidate_event_type,
                stage="judge",
                limit=asyncio.Semaphore(_ONE_CALL),
                traceparent=ctx.traceparent,
            )
        except NonRetryableError:
            logger.warning(
                "alert judgment rejected by ai-gateway",
                extra={"candidateId": str(message.candidate_id)},
            )
            return Outcome.failed("judge", message.candidate_event_type, "AI_CALL_REJECTED")

    def _closing(self, message: AlertCandidate, outcome: Outcome, default: Severity) -> AlertRaised:
        analysis = outcome.analysis
        if analysis.verification_status == "confirmed" and analysis.event_detected is True:
            return self._raised(
                message, "confirmed", analysis.severity or default, analysis=analysis
            )
        return self._raised(message, "dismissed", default, dismiss=_dismiss_reason(analysis))

    def _raised(
        self,
        message: AlertCandidate,
        status: AlertStatus,
        severity: Severity,
        *,
        analysis: VLMAnalysis | None = None,
        dismiss: DismissReason | None = None,
    ) -> AlertRaised:
        return AlertRaised(
            event_id=ids.alert_event_id(message.candidate_id, status),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            job_id=message.job_id,
            camera_id=message.camera_id,
            candidate_id=message.candidate_id,
            status=status,
            candidate_event_type=message.candidate_event_type,
            reason=message.reason,
            severity=severity,
            track_id=message.track_id,
            zone_id=message.zone_id,
            observed_ms=message.observed_ms,
            evidence_frame_keys=message.evidence_frame_keys,
            analysis=analysis,
            dismiss_reason=dismiss,
        )

    def _publish(self, event: AlertRaised, traceparent: str | None) -> None:
        publish(self._publisher, event, self._clock, traceparent, key=str(event.source_id))


def _dismiss_reason(analysis: VLMAnalysis) -> DismissReason:
    """R-23: 예산·모델 미설정은 unverified, 출력 검증·호출 실패는 unknown, 그 밖은 rejected."""
    status = analysis.verification_status
    if status == "unverified":
        return "unverified"
    if status == "unknown":
        return "unknown"
    return "rejected"
