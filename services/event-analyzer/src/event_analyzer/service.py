"""`event.candidate.vector` 하나를 후보마다 판정해 `event.analyzed`로 발행한다(SPEC-020)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse, ImageRef
from vlm_contracts.events import (
    Event,
    EventAnalyzed,
    EventCandidateVector,
    VectorCandidate,
    VectorEvidence,
    VLMAnalysis,
)
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext, NonRetryableError

from event_analyzer import ids
from event_analyzer.judgment import Decision, Outcome, Stage, decide, needs_escalation

_TASK_TYPES: dict[Stage, str] = {"judge": "event_judge", "escalate": "event_judge_escalate"}
# 이미지가 없거나 분당 한도 차례를 얻지 못하면 그 후보만 판정 실패로 남긴다(SPEC-020 R-14).
# 분당 한도는 메시지 전체의 문제가 아니라서 DLQ로 보내지 않는다.
_RECORDED_CLIENT_ERRORS = frozenset({"AI_IMAGE_NOT_FOUND", "AI_RATE_LIMITED"})
_INVALID_RESPONSE = "AI_INVALID_RESPONSE"

logger = logging.getLogger(__name__)


class AnalyzeClient(Protocol):
    """ai-gateway `/v1/analyze` 호출. 운영은 `vlm_ai_client.client.AiGatewayClient`다."""

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse: ...


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class AnalyzerConfig:
    """판정 설정.

    Attributes:
        concurrency: 한 메시지 안에서 동시에 진행하는 판정 호출 수(SPEC-020 R-18). 후보가 최대
            512개라 묶지 않으면 ai-gateway 분당 한도를 한 메시지가 다 쓴다.
    """

    concurrency: int = 4

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")


class EventAnalyzerService:
    """후보마다 `event_judge`로 판정하고, 위험하거나 애매한 후보는 `event_judge_escalate`로
    다시 판정한 뒤 후보마다 `event.analyzed`를 발행한다.

    모든 판정이 끝난 뒤에 발행하므로, 메시지를 DLQ로 보내는 오류가 나면 발행된 것이 없다.
    Kafka 장애는 예외를 그대로 던져 워커가 다시 시도하게 한다(SPEC-020 R-17).
    """

    def __init__(
        self,
        client: AnalyzeClient,
        publisher: EventPublisher,
        config: AnalyzerConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._publisher = publisher
        self._config = config
        self._clock = clock

    async def handle(self, message: EventCandidateVector, ctx: MessageContext) -> None:
        """워커 경로 핸들러. 판정 호출은 I/O라 이벤트 루프에서 동시에 기다린다."""
        limit = asyncio.Semaphore(self._config.concurrency)
        candidates = message.candidates
        with ctx.timing.stage("analyzer.judge", unit="candidates") as block:
            firsts = await _gather(
                [self._judge(message, c, "judge", limit, ctx.traceparent) for c in candidates]
            )
            block.count = len(candidates)
        escalated: dict[int, Outcome] = {}
        risky = [i for i, first in enumerate(firsts) if needs_escalation(first)]
        if risky:
            with ctx.timing.stage("analyzer.escalate", unit="candidates") as block:
                seconds = await _gather(
                    [
                        self._judge(message, candidates[i], "escalate", limit, ctx.traceparent)
                        for i in risky
                    ]
                )
                escalated = dict(zip(risky, seconds, strict=True))
                block.count = len(risky)
        for i, (candidate, first) in enumerate(zip(candidates, firsts, strict=True)):
            decision = decide(first, escalated.get(i))
            await asyncio.to_thread(
                self._publish, self._analyzed(message, candidate, decision), ctx.traceparent
            )

    async def _judge(
        self,
        message: EventCandidateVector,
        candidate: VectorCandidate,
        stage: Stage,
        limit: asyncio.Semaphore,
        traceparent: str | None,
    ) -> Outcome:
        """판정 호출 하나. 결과를 못 받은 호출은 `Outcome.failed`로 남긴다(R-12~R-14).

        Raises:
            NonRetryableError: ai-gateway가 `AI_IMAGE_NOT_FOUND`·`AI_RATE_LIMITED` 외의 4xx로
                거절했을 때(R-15).
                호출자·경로·taskType 설정 문제라 같은 메시지의 다른 후보도 같이 거절된다.
        """
        try:
            async with limit:
                response = await self._client.analyze(
                    _request(message, candidate, _TASK_TYPES[stage]), traceparent=traceparent
                )
            analysis = _analysis(response)
        except AiGatewayError as e:
            if _rejects_message(e):
                raise NonRetryableError(f"ai-gateway rejected the call: {e.code}") from e
            logger.warning(
                "judgment call failed",
                extra={"candidateId": str(candidate.candidate_id), "stage": stage, "code": e.code},
            )
            return Outcome.failed(stage, candidate.event_type, e.code)
        return Outcome.called(
            stage, response.call_id, response.model_id, response.prompt_version, analysis
        )

    def _analyzed(
        self, message: EventCandidateVector, candidate: VectorCandidate, decision: Decision
    ) -> EventAnalyzed:
        return EventAnalyzed(
            event_id=ids.analyzed_event_id(candidate.candidate_id),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            job_id=message.job_id,
            chunk_id=message.chunk_id,
            execution_id=message.execution_id,
            normalization_id=message.normalization_id,
            candidate_id=candidate.candidate_id,
            candidate_event_type=candidate.event_type,
            start_ms=candidate.start_ms,
            end_ms=candidate.end_ms,
            evidence_frame_keys=candidate.frame_keys,
            judgment_method=decision.method,
            vector_candidate=VectorEvidence(
                segment_id=candidate.segment_id,
                score=candidate.score,
                threshold=candidate.threshold,
                prototype_version=message.prototype_version,
                embedding_model=message.embedding_model,
            ),
            analysis=decision.analysis,
            judgments=decision.judgments,
            review_required=decision.review_required,
            review_reasons=decision.review_reasons,
        )

    def _publish(self, event: Event, traceparent: str | None) -> None:
        self._publisher.publish(
            event.event_type, prepare_event(event, self._clock()), traceparent=traceparent
        )


def _request(
    message: EventCandidateVector, candidate: VectorCandidate, task_type: str
) -> AnalyzeRequest:
    """판정 요청. 재판정도 같은 입력이다(SPEC-020 R-1~R-3)."""
    return AnalyzeRequest(
        task_type=task_type,
        source_id=message.source_id,
        images=[ImageRef(storage_key=key) for key in candidate.frame_keys],
        variables={
            "eventType": candidate.event_type,
            "startSec": candidate.start_ms / 1000,
            "endSec": candidate.end_ms / 1000,
            # 벡터 후보는 구역 판정을 거치지 않았다.
            "zoneName": None,
            "evidence": {
                "path": "vector",
                "score": candidate.score,
                "threshold": candidate.threshold,
                "prototypeVersion": message.prototype_version,
                "embeddingModel": message.embedding_model,
            },
        },
    )


def _analysis(response: AnalyzeResponse) -> VLMAnalysis:
    try:
        return VLMAnalysis.model_validate(response.output)
    except ValidationError as e:
        # ai-gateway가 출력 스키마로 검증해 돌려주므로 계약이 어긋난 경우다. 후보는 버리지 않는다.
        raise AiGatewayError(
            _INVALID_RESPONSE,
            "analysis output does not match VLMAnalysis",
            status=200,
            retryable=False,
        ) from e


def _rejects_message(error: AiGatewayError) -> bool:
    status = error.status
    is_client_error = status is not None and 400 <= status < 500
    return is_client_error and error.code not in _RECORDED_CLIENT_ERRORS


async def _gather(calls: Sequence[Awaitable[Outcome]]) -> list[Outcome]:
    return list(await asyncio.gather(*calls))
