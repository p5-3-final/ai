"""`attribute.requested` 하나를 ai-gateway로 분석해 `track.analyzed`로 발행한다(SPEC-024)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse, ImageRef
from vlm_contracts.events import (
    AttributeRequested,
    Event,
    ObjectAnswer,
    TrackAnalyzed,
    TrackAttribute,
)
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext

from track_analyzer import ids

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


class TrackAnalyzerService:
    """요청 하나에 ai-gateway를 한 번 부르고 `track.analyzed`를 한 건 발행한다."""

    def __init__(
        self,
        client: AnalyzeClient,
        publisher: EventPublisher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._publisher = publisher
        self._clock = clock

    async def handle(self, message: AttributeRequested, ctx: MessageContext) -> None:
        """워커 경로 핸들러."""
        request = _request(message)
        try:
            with ctx.timing.stage("attribute.call", unit="images") as block:
                block.count = len(request.images)
                response = await self._client.analyze(request, traceparent=ctx.traceparent)
            event = self._ready(message, response)
        except AiGatewayError as e:
            # R-4: 화면이 기다리는 요청이라 4xx도 DLQ로 보내지 않고 FAILED로 알린다
            # (SPEC-020과 다름). NFR-3: 질문·답·요청 변수는 로그에 싣지 않는다.
            logger.warning(
                "attribute call failed",
                extra={
                    "requestId": str(message.request_id),
                    "reason": message.reason,
                    "taskType": request.task_type,
                    "code": e.code,
                },
            )
            event = self._analyzed(message, outcome="FAILED", error_code=e.code)
        # 발행은 블로킹이라 이벤트 루프 밖에서 한다.
        await asyncio.to_thread(self._publish, event, ctx.traceparent)

    def _ready(self, message: AttributeRequested, response: AnalyzeResponse) -> TrackAnalyzed:
        """결과가 있는 경우(R-3). `unverified`·`unknown`도 결과다.

        Raises:
            AiGatewayError: 응답이 결과 형식·계약과 맞지 않을 때(`AI_INVALID_RESPONSE`).
        """
        is_question = message.reason == "question"
        try:
            return self._analyzed(
                message,
                outcome="READY",
                attributes=None if is_question else TrackAttribute.model_validate(response.output),
                answer=ObjectAnswer.model_validate(response.output) if is_question else None,
                response=response,
            )
        except ValidationError as e:
            # ai-gateway가 출력 스키마로 검증해 돌려주므로 계약이 어긋난 경우다. 예외 메시지에
            # 출력 원문이 들어가지 않도록 원인 예외의 내용은 싣지 않는다(NFR-3).
            raise AiGatewayError(
                _INVALID_RESPONSE,
                "analysis output does not match the track result shape",
                status=200,
                retryable=False,
            ) from e

    def _analyzed(
        self,
        message: AttributeRequested,
        *,
        outcome: Literal["READY", "FAILED"],
        attributes: TrackAttribute | None = None,
        answer: ObjectAnswer | None = None,
        response: AnalyzeResponse | None = None,
        error_code: str | None = None,
    ) -> TrackAnalyzed:
        return TrackAnalyzed(
            event_id=ids.analyzed_event_id(message.request_id),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            request_id=message.request_id,
            job_id=message.job_id,
            track_id=message.track_id,
            reason=message.reason,
            outcome=outcome,
            attributes=attributes,
            answer=answer,
            call_id=response.call_id if response else None,
            model_id=response.model_id if response else None,
            prompt_version=response.prompt_version if response else None,
            # 계약은 v2만 허용한다. 다른 값이면 검증 오류로 잘못된 응답이 된다.
            taxonomy_version=response.taxonomy_version if response else None,
            error_code=error_code,
        )

    def _publish(self, event: TrackAnalyzed, traceparent: str | None) -> None:
        self._publisher.publish(
            event.event_type,
            prepare_event(event, self._clock()),
            key=str(event.source_id),
            traceparent=traceparent,
        )


def _request(message: AttributeRequested) -> AnalyzeRequest:
    """reason별 분석 요청(R-1, R-2). 이미지는 크롭 다음에 장면 프레임이고 순서를 지킨다."""
    images = [ImageRef(storage_key=c.key, pts_sec=c.pts_ms / 1000) for c in message.crops]
    variables: dict[str, object] = {"className": message.class_name}
    if message.reason != "question":
        return AnalyzeRequest(
            task_type="track_attribute",
            source_id=message.source_id,
            images=images,
            variables=variables,
        )
    # 장면 프레임의 시각은 계약에 없다.
    images += [ImageRef(storage_key=key) for key in message.evidence_frame_keys]
    return AnalyzeRequest(
        task_type="object_question",
        source_id=message.source_id,
        images=images,
        variables={**variables, "question": message.question},
    )
