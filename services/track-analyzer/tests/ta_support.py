"""track-analyzer 테스트가 함께 쓰는 메시지·가짜 ai-gateway·계약 검증 도우미."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse
from vlm_contracts.events import AttributeCrop, AttributeReason, AttributeRequested, Event
from vlm_testing.contracts import event_errors as shared_event_errors

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f100-0000-7000-8000-000000000001")
TRACK = UUID("0192f100-0000-5000-8000-0000000000e1")
REQUEST = UUID("0192f100-0000-5000-8000-0000000000a2")
MESSAGE_ID = UUID("0192f100-0000-5000-8000-0000000000a1")
NOW = datetime(2026, 11, 6, 9, 10, tzinfo=UTC)
PREFIX = (
    f"sources/{SOURCE}/jobs/{JOB}/chunks/0192f100-0000-7000-8000-000000000002"
    "/executions/0192f100-0000-7000-8000-000000000003/"
)
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
QUESTION = "이 사람이 손에 무엇을 들고 있나요?"


def crop(index: int) -> AttributeCrop:
    return AttributeCrop(key=f"{PREFIX}blurred/crop-t7-{index}.jpg", pts_ms=110_000 + index * 500)


def scene_key(index: int) -> str:
    return f"{PREFIX}blurred/scene-{index}.jpg"


def attribute_request(
    reason: AttributeReason = "click",
    *,
    crops: int = 2,
    scenes: int = 0,
    question: str | None = None,
    request_id: UUID = REQUEST,
) -> AttributeRequested:
    """`reason`에 맞는 요청. question이면 질문이 기본값으로 들어간다."""
    if reason == "question" and question is None:
        question = QUESTION
    return AttributeRequested(
        event_id=MESSAGE_ID,
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        request_id=request_id,
        job_id=JOB,
        track_id=TRACK,
        class_name="person",
        reason=reason,
        crops=[crop(i) for i in range(crops)],
        evidence_frame_keys=[scene_key(i) for i in range(scenes)],
        question=question,
    )


ATTRIBUTES: dict[str, object] = {
    "verificationStatus": "confirmed",
    "helmet": "not_worn",
    "vest": "worn",
    "role": "worker",
    "equipment": [],
    "description": "주황색 안전조끼를 입고 안전모를 쓰지 않은 작업자",
}
ANSWER: dict[str, object] = {
    "verificationStatus": "confirmed",
    "answerable": True,
    "answer": "오른손에 무전기를 들고 있습니다.",
}


def contract_output(request: AnalyzeRequest) -> dict[str, object]:
    """taskType에 맞는 확정 결과."""
    return ATTRIBUTES if request.task_type == "track_attribute" else ANSWER


type Reply = Callable[[AnalyzeRequest], dict[str, object] | AiGatewayError]


@dataclass
class FakeGateway:
    """`reply`로 결과나 오류를 정하는 가짜 ai-gateway. 받은 요청을 남긴다."""

    reply: Reply = contract_output
    taxonomy_version: str | None = "v2"
    requests: list[tuple[AnalyzeRequest, str | None]] = field(default_factory=list)

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse:
        self.requests.append((request, traceparent))
        output = self.reply(request)
        if isinstance(output, AiGatewayError):
            raise output
        unverified = output.get("verificationStatus") == "unverified"
        return AnalyzeResponse(
            call_id=UUID(int=len(self.requests)),
            task_type=request.task_type,
            model_id=None if unverified else f"{request.task_type}-m",
            prompt_version=f"{request.task_type}@0123456789ab",
            taxonomy_version=self.taxonomy_version,
            output=output,
        )


def gateway_error(code: str, status: int | None, *, retryable: bool = False) -> AiGatewayError:
    return AiGatewayError(code, code, status=status, retryable=retryable)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.published.append((topic, event, key, traceparent))


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    return shared_event_errors(event)
