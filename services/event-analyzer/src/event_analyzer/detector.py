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
    UnverifiedReason,
    VLMAnalysis,
)
from vlm_messaging.worker import MessageContext
from vlm_storage.storage import ObjectStorage

from event_analyzer import ids
from event_analyzer.calls import AnalyzeClient, EventPublisher, judge, publish
from event_analyzer.candidates import load_candidates
from event_analyzer.cap import call_cap
from event_analyzer.carry import TrackKey, plan_carry
from event_analyzer.judgment import Decision, Outcome, Stage, decide, needs_escalation
from event_analyzer.merge import Kept, merge_candidates
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rejudge_state import NoRejudgeState, Prior, RejudgeState
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


# 재판정 간격 상태의 TTL은 간격의 두 배다(SPEC-023 R-18). 다음 창이 늦어도 간격 안의 판정을 잇는다.
_STATE_TTL_INTERVALS = 2


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
        *,
        rejudge: RejudgeState | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._publisher = publisher
        self._storage = storage
        self._profile = profile
        self._config = config
        self._rejudge: RejudgeState = rejudge or NoRejudgeState()
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
            block.count = len(candidates)
        # 실시간은 창을 넘어 직전 판정을 잇고, 업로드는 메시지 안에서만 잇는다(R-17, R-18).
        stream = message.job_type == "STREAM"
        priors = await self._read_priors(message, kept) if stream else {}
        carry = plan_carry(
            [k.candidate for k in kept],
            self._profile.rejudge,
            {key: prior.start_ms for key, prior in priors.items()},
        )
        targets = [
            k
            for k in kept
            if k.candidate.judgment == "vlm" and k.candidate.candidate_id not in carry
        ]
        decisions |= await self._judge_all(message, targets, ctx)
        results = [_result(k, decisions, carry, priors) for k in kept]
        analyzed = [self._analyzed(message, r) for r in results]
        for event in analyzed:
            await asyncio.to_thread(self._publish, event, ctx.traceparent)
        manifest = self._manifest(message, len(candidates), analyzed)
        await asyncio.to_thread(self._publish, manifest, ctx.traceparent)
        # 발행 뒤에 쓴다. 발행이 실패해 다시 처리할 때 이번 판정을 직전 판정으로 읽지 않는다.
        if stream:
            await self._remember(message, results)

    async def _read_priors(
        self, message: EventCandidate, kept: list[Kept]
    ) -> dict[TrackKey, Prior]:
        keys = sorted(
            {
                (k.candidate.track_local_id, k.candidate.event_type)
                for k in kept
                if self._stateful(k.candidate) and k.candidate.track_local_id is not None
            }
        )
        return await self._rejudge.read(message.source_id, keys)

    async def _remember(self, message: EventCandidate, results: list[_Result]) -> None:
        """같은 (트랙, 유형)의 가장 늦은 `confirmed` 판정을 남긴다(R-18)."""
        latest: dict[TrackKey, Prior] = {}
        for result in sorted(results, key=lambda r: _start_order(r.kept)):
            candidate = result.kept.candidate
            judged = result.decision.method in {"rule+vlm", "rule+vlm+escalate"}
            confirmed = result.decision.analysis.verification_status == "confirmed"
            track = candidate.track_local_id
            if judged and confirmed and track is not None and self._stateful(candidate):
                latest[(track, candidate.event_type)] = Prior(
                    candidate.start_ms,
                    ids.analyzed_event_id(candidate.candidate_id),
                    result.decision.analysis,
                )
        for key, prior in latest.items():
            ttl_s = self._profile.rejudge[key[1]] * _STATE_TTL_INTERVALS
            await self._rejudge.write(message.source_id, key, prior, ttl_s)

    def _stateful(self, candidate: DetectorCandidate) -> bool:
        return (
            candidate.judgment == "vlm"
            and self._profile.rejudge_interval_s(candidate.event_type) is not None
        )

    async def _judge_all(
        self, message: EventCandidate, targets: list[Kept], ctx: MessageContext
    ) -> dict[UUID, Decision]:
        """상한 안에서 1차 판정을 모두 끝낸 뒤 남은 횟수로 재판정한다.

        1차 판정·재판정 모두 시작순으로 상한을 나눠 같은 메시지면 같은 후보가 판정된다(R-19).
        단계 측정 두 개가 spec대로 나뉜다(SPEC-020 plan).
        """
        ordered = sorted(
            targets, key=lambda k: (k.candidate.start_ms, str(k.candidate.candidate_id))
        )
        cap = call_cap(message.owned_range, self._profile.cap)
        called, over = ordered[:cap], ordered[cap:]
        decisions = {
            k.candidate.candidate_id: decide(Outcome.capped(k.candidate.event_type), None, "rule")
            for k in over
        }
        if not called:
            return decisions
        limit = asyncio.Semaphore(self._config.concurrency)
        with ctx.timing.stage("analyzer.judge", unit="candidates") as block:
            firsts = await asyncio.gather(
                *(self._call(message, k.candidate, "judge", limit, ctx) for k in called)
            )
            block.count = len(called)
        risky = [
            i
            for i, (k, first) in enumerate(zip(called, firsts, strict=True))
            if needs_escalation(first, ambiguous=self._ambiguous(k.candidate))
        ][: cap - len(called)]
        escalated: dict[int, Outcome] = {}
        if risky:
            with ctx.timing.stage("analyzer.escalate", unit="candidates") as block:
                seconds = await asyncio.gather(
                    *(
                        self._call(message, called[i].candidate, "escalate", limit, ctx)
                        for i in risky
                    )
                )
                escalated = dict(zip(risky, seconds, strict=True))
                block.count = len(risky)
        return decisions | {
            k.candidate.candidate_id: decide(first, escalated.get(i), path="rule")
            for i, (k, first) in enumerate(zip(called, firsts, strict=True))
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
            # 계약에서 선택이지만 소비자가 합산하므로 0이어도 싣는다(SPEC-023 R-20). 사유별 수는
            # 그 사유로 unverified가 된 결과를 복사한 rule+carry도 센다.
            carried_count=sum(e.judgment_method == "rule+carry" for e in analyzed),
            cap_reached_count=_reason_count(analyzed, "call_cap_reached"),
            budget_exceeded_count=_reason_count(analyzed, "budget_exceeded"),
        )

    def _publish(self, event: Event, traceparent: str | None) -> None:
        # AsyncAPI: 키는 sourceId다. 같은 영상의 이벤트가 한 파티션에서 순서를 지킨다.
        publish(self._publisher, event, self._clock, traceparent, key=str(event.source_id))


def _reason_count(events: list[EventAnalyzed], reason: UnverifiedReason) -> int:
    return sum(e.unverified_reason == reason for e in events)


def _start_order(kept: Kept) -> tuple[int, str]:
    return kept.candidate.start_ms, str(kept.candidate.candidate_id)


def _result(
    kept: Kept,
    decisions: dict[UUID, Decision],
    carry: dict[UUID, UUID | None],
    priors: dict[TrackKey, Prior],
) -> _Result:
    candidate = kept.candidate
    if candidate.candidate_id not in carry:
        return _Result(kept, decisions[candidate.candidate_id])
    source = carry[candidate.candidate_id]
    if source is not None:
        return _Result(kept, _carried(decisions[source]), ids.analyzed_event_id(source))
    # plan_carry는 직전 판정이 있는 (트랙, 유형)에만 None을 준다.
    prior = priors[(candidate.track_local_id or "", candidate.event_type)]
    return _Result(kept, _rule_carry(prior.analysis), prior.event_id)


def _carried(source: Decision) -> Decision:
    """직전 판정 결과를 그대로 잇는다. 호출하지 않았으므로 호출 기록은 없다(SPEC-023 R-17).

    직전 판정이 unverified·unknown이어도 같은 상태를 복사하고 다음 간격에서 다시 판정한다.
    """
    return replace(source, method="rule+carry", judgments=[])


def _rule_carry(analysis: VLMAnalysis) -> Decision:
    """창을 넘어 이은 직전 `confirmed` 판정(R-18)."""
    return Decision(analysis, "rule+carry", [], False, [])


def _rule_decision(candidate: DetectorCandidate, profile: EventAnalyzerProfile) -> Decision:
    """규칙 후보는 호출 없이 확정한다(SPEC-023 R-4)."""
    return Decision(rule_analysis(candidate, profile), "rule", [], False, [])
