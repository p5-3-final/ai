"""embedding-worker 테스트가 함께 쓰는 메시지·가짜 ai-gateway·계약 검증 도우미."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import EmbedRequest, EmbedResponse
from vlm_contracts.events import (
    AttributeReason,
    Event,
    EventAnalyzed,
    ObjectAnswer,
    SegmentAnnotated,
    TrackAnalyzed,
    TrackAttribute,
)
from vlm_testing.contracts import contracts_dir as contracts_dir
from vlm_testing.contracts import event_errors as shared_event_errors
from vlm_testing.contracts import schema_errors

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f100-0000-7000-8000-000000000001")
TRACK = UUID("0192f100-0000-5000-8000-0000000000e1")
SEGMENT = UUID("0192f100-0000-7000-8000-0000000000a1")
ANNOTATION = UUID("0192f100-0000-5000-8000-000000000a52")
NOW = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
MODEL = "text-embedding-3-small"
DIMENSION = 4

DESCRIPTION = "적재 구역에서 작업자 한 명이 바닥에 넘어져 일어나지 못하고 있다."
RISK_REASON = "쓰러진 작업자가 지게차 통로에 있다."
SOLUTION = "통로 운행을 멈추고 작업자 상태를 확인한다."
TRACK_DESCRIPTION = "주황색 안전조끼를 입고 안전모를 쓰지 않은 작업자"
ANNOTATION_TEXT = "지게차 옆에서 작업자가 넘어지는 장면"


def _example(path: str, index: int = 0) -> dict[str, object]:
    schema = json.loads((contracts_dir() / path).read_text(encoding="utf-8"))
    return cast(list[dict[str, object]], schema["examples"])[index]


def event_analyzed(
    *,
    description: str | None = DESCRIPTION,
    risk_reason: str | None = RISK_REASON,
    solution: str | None = SOLUTION,
    severity: str | None = "high",
    verification_status: str = "confirmed",
) -> EventAnalyzed:
    """계약 예시(벡터 후보)에서 판정 문장만 바꾼 `event.analyzed`."""
    body = _example("events/event.analyzed.schema.json")
    analysis = cast(dict[str, object], body["analysis"])
    risk = risk_reason is not None
    body["analysis"] = {
        **analysis,
        "description": description,
        "riskDetected": risk,
        "riskType": "struck_by_vehicle" if risk else None,
        "riskReason": risk_reason,
        "solution": solution,
        "severity": severity,
        "verificationStatus": verification_status,
    }
    return EventAnalyzed.model_validate(body)


ATTRIBUTES: dict[str, object] = {
    "verificationStatus": "confirmed",
    "helmet": "not_worn",
    "vest": "worn",
    "role": "worker",
    "equipment": [],
    "description": TRACK_DESCRIPTION,
}


def track_analyzed(
    reason: AttributeReason = "click",
    *,
    outcome: str = "READY",
    attributes: dict[str, object] | None = None,
    track_id: UUID = TRACK,
) -> TrackAnalyzed:
    is_ready = outcome == "READY"
    is_question = reason == "question"
    return TrackAnalyzed(
        event_id=UUID("0192f100-0000-5000-8000-0000000000b1"),
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        request_id=UUID("0192f100-0000-5000-8000-0000000000a2"),
        job_id=JOB,
        track_id=track_id,
        reason=reason,
        outcome="READY" if is_ready else "FAILED",
        attributes=TrackAttribute.model_validate(attributes or ATTRIBUTES)
        if is_ready and not is_question
        else None,
        answer=ObjectAnswer(verification_status="confirmed", answerable=True, answer="무전기")
        if is_ready and is_question
        else None,
        call_id=UUID(int=1) if is_ready else None,
        model_id="track_attribute-m" if is_ready else None,
        prompt_version="track_attribute@0123456789ab" if is_ready else None,
        taxonomy_version="v2" if is_ready else None,
        error_code=None if is_ready else "AI_PROVIDER_UNAVAILABLE",
    )


def segment_annotated(text: str = ANNOTATION_TEXT) -> SegmentAnnotated:
    return SegmentAnnotated.model_validate(
        {**_example("events/segment.annotated.schema.json"), "text": text}
    )


def track_mapping(*track_ids: UUID) -> bytes:
    """계약 예시 번역표에서 최종 트랙 ID만 바꾼 파일 내용."""
    body = _example("tracks/v1/track-mapping.schema.json")
    [template] = cast(list[dict[str, object]], body["tracks"])
    body["tracks"] = [{**template, "trackId": str(t)} for t in (track_ids or (TRACK,))]
    return json.dumps(body).encode()


def mapping_key(source: UUID = SOURCE, job: UUID = JOB) -> str:
    return f"sources/{source}/jobs/{job}/tracks/mapping.json"


def unit_vector(dimension: int = DIMENSION) -> list[float]:
    return [1.0] + [0.0] * (dimension - 1)


type Reply = Callable[[EmbedRequest], EmbedResponse | AiGatewayError]


def contract_reply(request: EmbedRequest) -> EmbedResponse:
    return EmbedResponse(
        call_id=UUID(int=7),
        task_type=request.task_type,
        model_id=MODEL,
        dimension=DIMENSION,
        vectors=[unit_vector() for _ in request.texts or []],
    )


@dataclass
class FakeGateway:
    """`reply`로 결과나 오류를 정하는 가짜 ai-gateway. 받은 요청을 남긴다."""

    reply: Reply = contract_reply
    requests: list[tuple[EmbedRequest, str | None]] = field(default_factory=list)

    async def embed(
        self, request: EmbedRequest, *, traceparent: str | None = None
    ) -> EmbedResponse:
        self.requests.append((request, traceparent))
        result = self.reply(request)
        if isinstance(result, AiGatewayError):
            raise result
        return result


def gateway_error(code: str, status: int | None, *, retryable: bool = False) -> AiGatewayError:
    return AiGatewayError(code, code, status=status, retryable=retryable)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)
    fail_with: Exception | None = None

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.published.append((topic, event, key, traceparent))


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    return shared_event_errors(event)


def text_vector_file_errors(data: bytes) -> list[str]:
    """저장한 텍스트 벡터 파일을 docs 원본 스키마로 검증한다."""
    return schema_errors("vectors/v1/text-vector-file.schema.json", json.loads(data))
