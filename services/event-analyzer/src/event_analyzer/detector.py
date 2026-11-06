"""`event.candidate` 하나를 병합·규칙 확정·판정해 `event.analyzed`와 `event.manifest`로 발행한다
(SPEC-023).

판정 호출과 실패 처리는 벡터 경로(SPEC-020)와 같은 `calls.judge`를 쓴다. 모든 판정이 끝난 뒤에
발행하므로, 메시지를 DLQ로 보내는 오류가 나면 발행된 것이 없다.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from vlm_ai_client.models import AnalyzeRequest, ImageRef
from vlm_contracts.events import (
    DetectorCandidate,
    DetectorEvidence,
    Event,
    EventAnalyzed,
    EventCandidate,
    EventManifest,
)
from vlm_messaging.worker import MessageContext
from vlm_storage.storage import ObjectStorage

from event_analyzer import ids
from event_analyzer.calls import AnalyzeClient, EventPublisher, judge, publish
from event_analyzer.candidates import load_candidates
from event_analyzer.carry import plan_carry
from event_analyzer.judgment import Decision, Outcome, Stage, decide, needs_escalation
from event_analyzer.merge import Kept, merge_candidates
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rules import rule_analysis
from event_analyzer.service import AnalyzerConfig


def detector_request(candidate: DetectorCandidate, source: EventCandidate) -> AnalyzeRequest:
    """탐지기 후보의 1차 판정 요청(SPEC-023 R-5). 재판정도 같은 입력이다."""
    return AnalyzeRequest(
        task_type="event_judge",
        source_id=source.source_id,
        images=[ImageRef(storage_key=key) for key in candidate.evidence_frame_keys],
        variables={
            "eventType": candidate.event_type,
            "startSec": candidate.start_ms / 1000,
            "endSec": candidate.end_ms / 1000,
            "zoneName": candidate.zone_name,
            "evidence": {
                "path": "detector",
                "triggerClass": candidate.trigger_class,
                "score": candidate.score,
                "observationCount": candidate.observation_count,
                "detectorProfile": source.detector.detector_profile,
            },
        },
    )


@dataclass(frozen=True, slots=True)
class _Result:
    """남은 후보 하나의 발행할 판정."""

    kept: Kept
    decision: Decision
    # rule+carry면 결과를 복사한 원본 event.analyzed의 eventId(SPEC-023 R-17)
    carried_from: UUID | None = None


class DetectorAnalyzerService:
    """탐지기·규칙 후보 경로 핸들러.

    Kafka 장애는 예외를 그대로 던져 워커가 다시 시도하게 한다(SPEC-023 R-15). 병합·판정이
    결정적이라 다시 처리하면 같은 eventId로 발행한다.
    """

    def __init__(
        self,
        client: AnalyzeClient,
        publisher: EventPublisher,
        storage: ObjectStorage,
        profile: EventAnalyzerProfile,
        config: AnalyzerConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._publisher = publisher
        self._storage = storage
        self._profile = profile
        self._config = config
        self._clock = clock

    async def handle(self, message: EventCandidate, ctx: MessageContext) -> None:
        """워커 경로 핸들러."""
        candidates = await asyncio.to_thread(load_candidates, message, self._storage)
        # 후보가 0개여도 블록을 남겨 처리한 메시지 수가 단계 측정에 드러나게 한다.
        with ctx.timing.stage("analyzer.merge", unit="candidates") as block:
            kept = merge_candidates(candidates, self._profile.merge.cooldown_s)
            decisions = {
                k.candidate.candidate_id: _rule_decision(k.candidate, self._profile)
                for k in kept
                if k.candidate.judgment == "rule"
            }
            carry = plan_carry([k.candidate for k in kept], self._profile.rejudge)
            block.count = len(candidates)
        targets = [
            k
            for k in kept
            if k.candidate.judgment == "vlm" and k.candidate.candidate_id not in carry
        ]
        decisions |= await self._judge_all(message, targets, ctx)
        analyzed = [self._analyzed(message, _result(k, decisions, carry)) for k in kept]
        for event in analyzed:
            await asyncio.to_thread(self._publish, event, ctx.traceparent)
        manifest = self._manifest(message, len(candidates), analyzed)
        await asyncio.to_thread(self._publish, manifest, ctx.traceparent)

    async def _judge_all(
        self, message: EventCandidate, vlm: list[Kept], ctx: MessageContext
    ) -> dict[UUID, Decision]:
        """1차 판정을 모두 끝낸 뒤 재판정한다. 단계 측정 두 개가 spec대로 나뉜다(SPEC-020 plan)."""
        if not vlm:
            return {}
        limit = asyncio.Semaphore(self._config.concurrency)
        with ctx.timing.stage("analyzer.judge", unit="candidates") as block:
            firsts = await asyncio.gather(
                *(self._call(message, k.candidate, "judge", limit, ctx) for k in vlm)
            )
            block.count = len(vlm)
        risky = [
            i
            for i, (k, first) in enumerate(zip(vlm, firsts, strict=True))
            if needs_escalation(first, ambiguous=self._ambiguous(k.candidate))
        ]
        escalated: dict[int, Outcome] = {}
        if risky:
            with ctx.timing.stage("analyzer.escalate", unit="candidates") as block:
                seconds = await asyncio.gather(
                    *(self._call(message, vlm[i].candidate, "escalate", limit, ctx) for i in risky)
                )
                escalated = dict(zip(risky, seconds, strict=True))
                block.count = len(risky)
        return {
            k.candidate.candidate_id: decide(first, escalated.get(i), path="rule")
            for i, (k, first) in enumerate(zip(vlm, firsts, strict=True))
        }

    async def _call(
        self,
        message: EventCandidate,
        candidate: DetectorCandidate,
        stage: Stage,
        limit: asyncio.Semaphore,
        ctx: MessageContext,
    ) -> Outcome:
        return await judge(
            self._client,
            detector_request(candidate, message),
            candidate_id=candidate.candidate_id,
            event_type=candidate.event_type,
            stage=stage,
            limit=limit,
            traceparent=ctx.traceparent,
        )

    def _ambiguous(self, candidate: DetectorCandidate) -> bool:
        return self._profile.is_ambiguous(candidate.event_type, candidate.score)

    def _analyzed(self, message: EventCandidate, result: _Result) -> EventAnalyzed:
        candidate, decision = result.kept.candidate, result.decision
        return EventAnalyzed(
            event_id=ids.analyzed_event_id(candidate.candidate_id),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            job_type=message.job_type,
            job_id=message.job_id,
            chunk_id=message.chunk_id,
            execution_id=message.execution_id,
            normalization_id=message.normalization_id,
            candidate_id=candidate.candidate_id,
            candidate_event_type=candidate.event_type,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            evidence_frame_keys=candidate.evidence_frame_keys,
            judgment_method=decision.method,
            detector_candidate=DetectorEvidence(
                chunk_id=message.chunk_id,
                execution_id=message.execution_id,
                trigger_class=candidate.trigger_class,
                score=candidate.score,
                observation_count=candidate.observation_count,
                track_local_id=candidate.track_local_id,
                zone_id=candidate.zone_id,
                zone_name=candidate.zone_name,
                detector_profile=message.detector.detector_profile,
                merged_candidate_ids=list(result.kept.merged_ids),
            ),
            analysis=decision.analysis,
            judgments=decision.judgments,
            review_required=decision.review_required,
            review_reasons=decision.review_reasons,
            unverified_reason=decision.unverified_reason,
            carried_from_event_id=result.carried_from,
        )

    def _manifest(
        self, message: EventCandidate, received: int, analyzed: list[EventAnalyzed]
    ) -> EventManifest:
        """실행 하나의 판정 완료(SPEC-023 R-10). pipeline-service가 완료 판정에 센다."""
        return EventManifest(
            event_id=ids.manifest_event_id(message.execution_id),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            job_type=message.job_type,
            job_id=message.job_id,
            chunk_id=message.chunk_id,
            execution_id=message.execution_id,
            normalization_id=message.normalization_id,
            candidate_count=received,
            merged_count=received - len(analyzed),
            analyzed_count=len(analyzed),
            analyzed_event_ids=[e.event_id for e in analyzed],
            unsupported_events=message.unsupported_events,
            detector_profile=message.detector.detector_profile,
        )

    def _publish(self, event: Event, traceparent: str | None) -> None:
        # AsyncAPI: 키는 sourceId다. 같은 영상의 이벤트가 한 파티션에서 순서를 지킨다.
        publish(self._publisher, event, self._clock, traceparent, key=str(event.source_id))


def _result(kept: Kept, decisions: dict[UUID, Decision], carry: dict[UUID, UUID | None]) -> _Result:
    candidate_id = kept.candidate.candidate_id
    if candidate_id not in carry:
        return _Result(kept, decisions[candidate_id])
    source = carry[candidate_id]
    if source is None:
        raise RuntimeError("carry from a prior judgment needs rejudge state")
    return _Result(kept, _carried(decisions[source]), ids.analyzed_event_id(source))


def _carried(source: Decision) -> Decision:
    """직전 판정 결과를 그대로 잇는다. 호출하지 않았으므로 호출 기록은 없다(SPEC-023 R-17).

    직전 판정이 unverified·unknown이어도 같은 상태를 복사하고 다음 간격에서 다시 판정한다.
    """
    return replace(source, method="rule+carry", judgments=[])


def _rule_decision(candidate: DetectorCandidate, profile: EventAnalyzerProfile) -> Decision:
    """규칙 후보는 호출 없이 확정한다(SPEC-023 R-4)."""
    return Decision(rule_analysis(candidate, profile), "rule", [], False, [])
