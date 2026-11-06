"""판정 호출과 발행. 벡터·탐지기·긴급 경로가 같은 실패 처리 규칙을 쓴다(SPEC-020 R-11~R-15).

호출이 결과 없이 끝나면 후보를 버리지 않고 `Outcome.failed`로 남긴다. 메시지 전체의 설정 문제(그
밖의 4xx)만 `NonRetryableError`로 메시지를 DLQ로 보낸다.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse, ImageRef
from vlm_contracts.events import DetectableEventType, Event, VLMAnalysis
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import NonRetryableError

from event_analyzer.judgment import Outcome, Stage

TASK_TYPES: dict[Stage, str] = {"judge": "event_judge", "escalate": "event_judge_escalate"}
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


def judge_request(
    *,
    source_id: UUID,
    event_type: DetectableEventType,
    start_ms: int,
    end_ms: int,
    zone_name: str | None,
    frame_keys: list[str],
    evidence: dict[str, object],
) -> AnalyzeRequest:
    """`event_judge` 요청. 탐지기 후보와 긴급 후보가 같은 모양으로 만들어 결과 캐시가 적중한다
    (SPEC-023 R-5, R-22). 이미지는 근거 프레임 순서 그대로다.
    """
    return AnalyzeRequest(
        task_type="event_judge",
        source_id=source_id,
        images=[ImageRef(storage_key=key) for key in frame_keys],
        variables={
            "eventType": event_type,
            "startSec": start_ms / 1000,
            "endSec": end_ms / 1000,
            "zoneName": zone_name,
            "evidence": evidence,
        },
    )


def for_stage(request: AnalyzeRequest, stage: Stage) -> AnalyzeRequest:
    """재판정은 taskType만 바꾼 같은 입력이다(SPEC-020 R-3)."""
    return request.model_copy(update={"task_type": TASK_TYPES[stage]})


async def judge(
    client: AnalyzeClient,
    request: AnalyzeRequest,
    *,
    candidate_id: UUID,
    event_type: DetectableEventType,
    stage: Stage,
    limit: asyncio.Semaphore,
    traceparent: str | None,
) -> Outcome:
    """판정 호출 하나. 결과를 못 받은 호출은 `Outcome.failed`로 남긴다(SPEC-020 R-12~R-14).

    Raises:
        NonRetryableError: ai-gateway가 `AI_IMAGE_NOT_FOUND`·`AI_RATE_LIMITED` 외의 4xx로
            거절했을 때(SPEC-020 R-15). 호출자·경로·taskType 설정 문제라 같은 메시지의 다른 후보도
            같이 거절된다.
    """
    try:
        async with limit:
            response = await client.analyze(for_stage(request, stage), traceparent=traceparent)
        analysis = _analysis(response)
    except AiGatewayError as e:
        if rejects_message(e):
            raise NonRetryableError(f"ai-gateway rejected the call: {e.code}") from e
        logger.warning(
            "judgment call failed",
            extra={"candidateId": str(candidate_id), "stage": stage, "code": e.code},
        )
        return Outcome.failed(stage, event_type, e.code)
    return Outcome.called(
        stage,
        response.call_id,
        response.model_id,
        response.prompt_version,
        analysis,
        unverified_reason=response.unverified_reason,
    )


def rejects_message(error: AiGatewayError) -> bool:
    """메시지 전체를 DLQ로 보낼 오류인지(SPEC-020 R-15)."""
    status = error.status
    is_client_error = status is not None and 400 <= status < 500
    return is_client_error and error.code not in _RECORDED_CLIENT_ERRORS


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


def publish(
    publisher: EventPublisher,
    event: Event,
    clock: Callable[[], datetime],
    traceparent: str | None,
    *,
    key: str | None = None,
) -> None:
    """발행 시각을 채워 발행한다. 블로킹이라 `asyncio.to_thread`로 부른다."""
    publisher.publish(
        event.event_type, prepare_event(event, clock()), key=key, traceparent=traceparent
    )
