"""event-analyzer 테스트가 함께 쓰는 메시지·가짜 ai-gateway·계약 검증 도우미."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from uuid import UUID, uuid5

from jsonschema import Draft202012Validator
from pydantic import Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from vlm_ai_client.client import AiGatewayError, AnalyzeRequest, AnalyzeResponse
from vlm_contracts.events import (
    DetectableEventType,
    Event,
    EventCandidateVector,
    Severity,
    VectorCandidate,
    VerificationStatus,
)

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
OWNER = UUID("0192f0c4-6d1e-7a3b-9c2d-1e2f3a4b5c6d")
JOB = UUID("0192f100-0000-7000-8000-000000000001")
CHUNK = UUID("0192f100-0000-7000-8000-000000000002")
EXECUTION = UUID("0192f100-0000-7000-8000-000000000003")
NORMALIZATION = UUID("0192f100-0000-7000-8000-000000000004")
MESSAGE_ID = UUID("0192f100-0000-7000-8000-000000000005")
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
EMBEDDING_MODEL = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
PROTOTYPE_VERSION = "local-1"
PREFIX = f"sources/{SOURCE}/jobs/{JOB}/chunks/{CHUNK}/executions/{EXECUTION}/"
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"


def frame_key(segment: int, frame: int) -> str:
    return f"{PREFIX}blurred/seg{segment:02d}_f{frame}.jpg"


def candidate(index: int, event_type: DetectableEventType = "fall") -> VectorCandidate:
    """`index`번째 구간(10초)의 후보. ID는 frame-encoder처럼 구간·유형으로 정해진다."""
    segment = UUID(int=0x1000 + index)
    return VectorCandidate(
        candidate_id=uuid5(segment, event_type),
        segment_id=segment,
        start_ms=index * 10_000,
        end_ms=(index + 1) * 10_000,
        event_type=event_type,
        score=0.31,
        threshold=0.25,
        frame_keys=[frame_key(index, f) for f in range(4)],
    )


def candidates_message(*candidates: VectorCandidate) -> EventCandidateVector:
    return EventCandidateVector(
        event_id=MESSAGE_ID,
        occurred_at=NOW,
        source_id=SOURCE,
        owner_id=OWNER,
        job_id=JOB,
        chunk_id=CHUNK,
        execution_id=EXECUTION,
        normalization_id=NORMALIZATION,
        embedding_model=EMBEDDING_MODEL,
        prototype_version=PROTOTYPE_VERSION,
        candidates=list(candidates) or [candidate(0)],
    )


def judged(
    event_type: str,
    status: VerificationStatus = "confirmed",
    severity: Severity | None = "medium",
) -> dict[str, object]:
    """ai-gateway가 돌려주는 VLMAnalysis. unverified·unknown이면 판정 필드가 null이다."""
    decided = status in {"confirmed", "rejected"}
    return {
        "eventType": event_type,
        "eventDetected": (status == "confirmed") if decided else None,
        "verificationStatus": status,
        "description": "작업자가 바닥에 넘어졌다." if decided else None,
        "beforeState": None,
        "afterState": None,
        "riskDetected": False if decided else None,
        "riskType": None,
        "riskReason": None,
        "solution": None,
        "severity": severity if decided else None,
        "evidenceStart": None,
        "evidenceEnd": None,
    }


type Reply = Callable[[AnalyzeRequest], dict[str, object] | AiGatewayError]


def medium_confirmed(request: AnalyzeRequest) -> dict[str, object]:
    return judged(str(request.variables["eventType"]))


@dataclass
class FakeGateway:
    """`reply`로 판정 결과나 오류를 정하는 가짜 ai-gateway. 받은 요청과 동시 호출 수를 남긴다."""

    reply: Reply = medium_confirmed
    # 호출마다 기다리는 시간. 동시 호출 수를 재려면 0보다 커야 한다.
    delay_s: float = 0.0
    requests: list[tuple[AnalyzeRequest, str | None]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse:
        self.requests.append((request, traceparent))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay_s)
        finally:
            self.in_flight -= 1
        output = self.reply(request)
        if isinstance(output, AiGatewayError):
            raise output
        number = len(self.requests)
        unverified = output.get("verificationStatus") == "unverified"
        model = None if unverified else f"{request.task_type}-m"
        return AnalyzeResponse(
            call_id=UUID(int=number),
            task_type=request.task_type,
            model_id=model,
            prompt_version=f"{request.task_type}@0123456789ab",
            taxonomy_version="v2",
            output=output,
        )

    def task_types(self) -> list[str]:
        return [r.task_type for r, _ in self.requests]


def gateway_error(code: str, status: int | None, *, retryable: bool = False) -> AiGatewayError:
    return AiGatewayError(code, code, status=status, retryable=retryable)


@dataclass
class RecordingPublisher:
    published: list[tuple[str, Event, str | None, str | None]] = field(default_factory=list)

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        self.published.append((topic, event, key, traceparent))


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


@cache
def _event_registry() -> tuple[Registry[Schema], str]:
    path = _ContractSettings().contracts_dir
    # 원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    assert (path / "events").is_dir(), f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)"
    # 스키마는 서로를 상대 경로로 참조하므로 각자의 $id로 등록한다.
    resources = []
    for schema in [
        *sorted((path / "events").glob("*.schema.json")),
        path / "taxonomy/v2/taxonomy.schema.json",
    ]:
        raw = json.loads(schema.read_text(encoding="utf-8"))
        resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    envelope = json.loads((path / "events/_envelope.schema.json").read_text(encoding="utf-8"))
    return Registry().with_resources(resources), str(envelope["$id"])


def event_errors(event: Event) -> list[str]:
    """발행한 이벤트를 JSON으로 바꿔 docs 원본 스키마로 검증하고 오류 메시지를 돌려준다."""
    registry, envelope_id = _event_registry()
    schema_id = envelope_id.replace("_envelope", event.event_type)
    validator = Draft202012Validator({"$ref": schema_id}, registry=registry)
    body = json.loads(event.model_dump_json(by_alias=True))
    return [e.message for e in validator.iter_errors(body)]
