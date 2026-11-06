"""이벤트 Pydantic 모델.

원본은 docs 레포 `service-contracts/events/*.schema.json`이고, 계약 테스트가 둘이 같은지 확인한다.
JSON 필드 이름은 camelCase, 파이썬 속성은 snake_case다.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class Event(BaseModel):
    """모든 이벤트가 담는 envelope(`_envelope.schema.json`).

    계약은 필드 추가만 허용하므로 모르는 필드는 무시한다. 받은 이벤트를 고쳐 쓰지 않도록 불변이다.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )

    event_id: UUID
    event_type: str
    schema_version: int = Field(ge=1)
    occurred_at: AwareDatetime
    source_id: UUID
    owner_id: UUID
    # 발행 라이브러리가 채우는 발행 시각(SPEC-013 R-8). 채우지 않는 생산자도 있어 선택 필드이고,
    # 없으면 JSON에서 빼서 그 생산자의 메시지 모양을 바꾸지 않는다.
    produced_at: AwareDatetime | None = Field(default=None, exclude_if=lambda v: v is None)


class VideoUploaded(Event):
    """영상이 업로드 완료(UPLOADED)로 바뀌었다. 발행: media-service."""

    event_type: Literal["video.uploaded"] = "video.uploaded"
    schema_version: Literal[1] = 1
    storage_key: str
    # 업로드 상한 5GiB (SPEC-003)
    size_bytes: int = Field(ge=1, le=5_368_709_120)
    content_type: Literal["video/mp4", "video/quicktime", "video/x-matroska", "video/x-msvideo"]
    # 작업 세션에 속한 영상만 둘 다 있다(media SPEC-017 R-11, R-12). 없으면 JSON에서 뺀다.
    session_id: UUID | None = Field(default=None, exclude_if=lambda v: v is None)
    role: Literal["BEFORE", "DURING", "AFTER"] | None = Field(
        default=None, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _session_complete(self) -> Self:
        if (self.session_id is None) != (self.role is None):
            raise ValueError("sessionId and role must be given together")
        return self


class Retention(BaseModel):
    """삭제한 영상의 학습용 보존 정보(media SPEC-018 R-8, ADR-0011)."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )

    retention_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    retain_until: AwareDatetime


class VideoDeleted(Event):
    """영상이 삭제(DELETED)로 바뀌었다. video.uploaded보다 먼저 올 수 있다. 발행: media-service."""

    event_type: Literal["video.deleted"] = "video.deleted"
    schema_version: Literal[1] = 1
    # 보존에 동의한 영상에만 있다. ai의 서비스 데이터(호출 로그·결과 캐시·벡터 파일)는 모두
    # 즉시 삭제 등급이라 이 값과 상관없이 지운다(ADR-0011 (a)).
    retention: Retention | None = Field(default=None, exclude_if=lambda v: v is None)


class VideoNormalizing(Event):
    """영상 정규화를 시작했다. 발행: normalize (SPEC-007 R-1)."""

    event_type: Literal["video.normalizing"] = "video.normalizing"
    schema_version: Literal[1] = 1


RejectReason = Literal[
    "UNREADABLE", "NO_VIDEO_STREAM", "MISSING_METADATA", "TOO_SHORT", "TOO_LONG", "DECODE_FAILED"
]
"""정규화 거절 사유 (SPEC-007 R-7~R-12)."""


class VideoNormalized(Event):
    """영상 정규화가 끝났다. 발행: normalize, 소비: media-service, pipeline-service.

    원본은 `video.normalized.v2.schema.json`이다(SPEC-007 R-6~R-14, SPEC-008, ADR-0009).
    모든 필드를 항상 싣고, 결과에 해당하지 않는 값은 None(JSON null)이다. 연동 시간은 정수
    밀리초(`duration_ms`)이고 `duration_sec`는 그 값을 1000으로 나눈 호환 표시다.
    """

    event_type: Literal["video.normalized"] = "video.normalized"
    schema_version: Literal[2] = 2
    normalization_id: UUID
    normalization_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    result: Literal["READY", "REJECTED"]
    reject_reason: RejectReason | None
    normalized_key: str | None = Field(min_length=1)
    hls_playlist_key: str | None = Field(min_length=1)
    duration_ms: int | None = Field(ge=1)
    duration_sec: float | None = Field(gt=0)
    fps: float | None = Field(gt=0, le=240)
    width: int | None = Field(ge=1)
    height: int | None = Field(ge=1)
    transcoded: bool | None

    @model_validator(mode="after")
    def _fields_match_result(self) -> Self:
        media = (
            self.normalized_key,
            self.hls_playlist_key,
            self.duration_ms,
            self.duration_sec,
            self.fps,
            self.width,
            self.height,
            self.transcoded,
        )
        # 계약의 oneOf: READY면 미디어 필드가 모두 있고 사유가 없다. REJECTED면 그 반대다.
        if self.result == "READY" and (self.reject_reason is not None or None in media):
            raise ValueError("READY requires all media fields and no rejectReason")
        if self.result == "REJECTED" and (
            self.reject_reason is None or any(v is not None for v in media)
        ):
            raise ValueError("REJECTED requires rejectReason and null media fields")
        # 소비자(pipeline)가 정확히 같은지 확인하므로 초 값은 밀리초에서만 만든다.
        if self.duration_ms is not None and self.duration_sec != self.duration_ms / 1000:
            raise ValueError("durationSec must equal durationMs / 1000")
        return self


class TimeRange(BaseModel):
    """정규화 영상 시작 기준 정수 ms, 시작 포함·끝 제외."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )
    start_ms: int = Field(ge=0, le=9223372036854775807)
    end_ms: int = Field(gt=0, le=9223372036854775807)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start_ms >= self.end_ms:
            raise ValueError("range must be increasing")
        return self


class ProcessingProfile(BaseModel):
    """SPEC-008에 저장된 불변 프로필 참조."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
    mode: Literal["production", "local"]
    inference_profile_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")


class ChunkRequested(Event):
    """SPEC-010 요청. 재전달은 실행 ID와 전체 입력을 유지한다."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["chunk.requested"] = "chunk.requested"
    schema_version: Literal[1] = 1
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    request_id: UUID
    normalization_id: UUID
    normalized_key: str
    owned_range: TimeRange
    processing_range: TimeRange
    processing_profile: ProcessingProfile
    start_before: AwareDatetime

    @model_validator(mode="after")
    def consistent(self) -> Self:
        prefix = f"sources/{self.source_id}/normalizations/{self.normalization_id}/"
        if (
            self.request_id != self.execution_id
            or self.start_before <= self.occurred_at
            or not self.normalized_key.startswith(prefix)
            or any(v in self.normalized_key for v in ("..", "?", "#"))
            or self.processing_range.start_ms > self.owned_range.start_ms
            or self.processing_range.end_ms < self.owned_range.end_ms
        ):
            raise ValueError("inconsistent chunk request")
        return self


class ChunkExecutionFinished(Event):
    """SPEC-014 실제 종료 이벤트. 불명 timeout에는 생성하지 않는다."""

    model_config = ConfigDict(extra="forbid")

    event_type: Literal["chunk.execution.finished"] = "chunk.execution.finished"
    schema_version: Literal[1] = 1
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    request_id: UUID
    normalization_id: UUID
    outcome: Literal["SUCCEEDED", "FAILED"]
    result_key: str | None
    error_code: str | None = Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")

    @model_validator(mode="after")
    def terminal(self) -> Self:
        expected = (
            f"sources/{self.source_id}/jobs/{self.job_id}/chunks/{self.chunk_id}"
            f"/executions/{self.execution_id}/result.json"
        )
        if self.request_id != self.execution_id:
            raise ValueError("requestId must equal executionId")
        if self.outcome == "SUCCEEDED":
            if self.result_key != expected or self.error_code is not None:
                raise ValueError("success requires immutable result reference and no error")
        elif self.result_key is not None or self.error_code is None:
            raise ValueError("failure requires error and no result reference")
        return self


_MS = Field(ge=0, le=9223372036854775807)
_UUID36 = r"[0-9a-f-]{36}"
_EXECUTION_PREFIX = rf"^sources/{_UUID36}/jobs/{_UUID36}/chunks/{_UUID36}/executions/{_UUID36}/"
_BLURRED_IMAGE = _EXECUTION_PREFIX + r"blurred/[^/?#]+\.(jpg|jpeg|png|webp)$"
# 분류 체계 v2의 탐지 가능 유형(other 제외). 원본은 taxonomy/v2/taxonomy.schema.json이다.
type DetectableEventType = Literal[
    "fall",
    "fire_or_smoke",
    "ppe_missing",
    "object_removed",
    "barrier_not_restored",
    "hazard_zone_access",
    "equipment_abandoned",
    "object_moved",
]
# 분류 체계 v2의 이벤트 유형(탐지 가능 유형과 목록 밖 사건 other)과 심각도
type EventType = DetectableEventType | Literal["other"]
type Severity = Literal["low", "medium", "high"]
type VerificationStatus = Literal["confirmed", "rejected", "unverified", "unknown"]
_MODEL_ID = r"^[^:\s]+:[^:\s]+:[^:\s]+:[^:\s]+$"
_PROTOTYPE_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$"


class _Part(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class SegmentFrame(_Part):
    """구간 안 프레임 하나(블러 사본)."""

    pts_ms: int = _MS
    key: str = Field(pattern=_BLURRED_IMAGE)


class ReadySegment(_Part):
    """`segment.ready`의 구간. 프레임은 정확히 4장이다."""

    segment_id: UUID
    start_ms: int = _MS
    end_ms: int = _MS
    static: bool
    frames: list[SegmentFrame] = Field(min_length=4, max_length=4)


class ReadyCrop(_Part):
    """트랙 대표 크롭(블러 사본)."""

    crop_id: UUID
    track_local_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    pts_ms: int = _MS
    key: str = Field(pattern=_BLURRED_IMAGE)


class _Execution(Event):
    """청크 실행 하나에 묶인 이벤트의 식별자."""

    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    normalization_id: UUID

    def execution_prefix(self) -> str:
        """이 실행의 저장소 폴더. 입력·출력 경로는 모두 이 아래다."""
        return (
            f"sources/{self.source_id}/jobs/{self.job_id}/chunks/{self.chunk_id}"
            f"/executions/{self.execution_id}/"
        )


class SegmentReady(_Execution):
    """청크의 검색 경로 입력 준비 완료. 발행: chunk-processor. 소비: frame-encoder(SPEC-019)."""

    event_type: Literal["segment.ready"] = "segment.ready"
    schema_version: Literal[1] = 1
    segments: list[ReadySegment] = Field(max_length=64)
    crops: list[ReadyCrop] = Field(max_length=512)

    @model_validator(mode="after")
    def _inside_execution_folder(self) -> Self:
        # SPEC-019 R-16: 다른 영상·실행의 이미지를 읽지 않는다. 스키마 패턴은 형식만 보므로
        # 메시지의 식별자와 맞는지 여기서 확인한다.
        prefix = self.execution_prefix() + "blurred/"
        keys = [f.key for s in self.segments for f in s.frames] + [c.key for c in self.crops]
        if any(not key.startswith(prefix) for key in keys):
            raise ValueError("image key is outside the execution folder of this message")
        return self


class SegmentEmbedded(_Execution):
    """구간·크롭 벡터 파일 저장 완료. 발행: frame-encoder(SPEC-019 R-4)."""

    event_type: Literal["segment.embedded"] = "segment.embedded"
    schema_version: Literal[1] = 1
    vectors_key: str = Field(pattern=_EXECUTION_PREFIX + r"vectors[.]json$")
    segment_count: int = Field(ge=0)
    crop_count: int = Field(ge=0)
    embedding_model: str = Field(pattern=_MODEL_ID)
    dimension: int = Field(ge=1, le=4096)


class VectorCandidate(_Part):
    """구간 벡터 프로토타입 후보 하나(SPEC-019 R-14)."""

    candidate_id: UUID
    segment_id: UUID
    start_ms: int = _MS
    end_ms: int = _MS
    event_type: DetectableEventType
    score: float = Field(ge=-1, le=1)
    threshold: float = Field(gt=-1, le=1)
    frame_keys: list[str] = Field(min_length=4, max_length=4)


class EventCandidateVector(_Execution):
    """구간 벡터 프로토타입 후보 묶음. 발행: frame-encoder(SPEC-019 R-12, ADR-0010 ③)."""

    event_type: Literal["event.candidate.vector"] = "event.candidate.vector"
    schema_version: Literal[1] = 1
    embedding_model: str = Field(pattern=_MODEL_ID)
    prototype_version: str = Field(pattern=_PROTOTYPE_VERSION)
    taxonomy_version: Literal["v2"] = "v2"
    candidates: list[VectorCandidate] = Field(min_length=1, max_length=512)


class VLMAnalysis(_Part):
    """이벤트 후보 판정 결과(`_vlm-analysis.schema.json`).

    ai-gateway `event_judge`·`event_judge_escalate`의 출력이고 `event.analyzed`의 `analysis`다.
    """

    event_type: EventType
    # None이면 판단할 수 없음
    event_detected: bool | None
    verification_status: VerificationStatus
    description: str | None = Field(max_length=1000)
    before_state: str | None = Field(max_length=500)
    after_state: str | None = Field(max_length=500)
    risk_detected: bool | None
    risk_type: str | None = Field(max_length=100)
    risk_reason: str | None = Field(max_length=500)
    solution: str | None = Field(max_length=500)
    severity: Severity | None
    evidence_start: float | None = Field(ge=0)
    evidence_end: float | None = Field(ge=0)

    @model_validator(mode="after")
    def _risk_explained(self) -> Self:
        # 계약의 if/then: 위험이 있다고 했으면 무엇이 왜 위험한지도 있어야 한다.
        if self.risk_detected and (self.risk_type is None or self.risk_reason is None):
            raise ValueError("riskType and riskReason are required when riskDetected is true")
        return self

    @classmethod
    def unknown(cls, event_type: EventType) -> "VLMAnalysis":
        """판정하지 못한 결과. 확정값 없이 유형만 후보의 것이다(SPEC-020 R-12)."""
        return cls(
            event_type=event_type,
            event_detected=None,
            verification_status="unknown",
            description=None,
            before_state=None,
            after_state=None,
            risk_detected=None,
            risk_type=None,
            risk_reason=None,
            solution=None,
            severity=None,
            evidence_start=None,
            evidence_end=None,
        )


class VectorEvidence(_Part):
    """벡터 후보의 근거(`event.analyzed`의 `vectorCandidate`)."""

    segment_id: UUID
    score: float = Field(ge=-1, le=1)
    threshold: float = Field(gt=-1, le=1)
    prototype_version: str = Field(pattern=_PROTOTYPE_VERSION)
    embedding_model: str = Field(pattern=_MODEL_ID)


class Judgment(_Part):
    """판정 호출 하나의 기록."""

    stage: Literal["judge", "escalate"]
    task_type: Literal["event_judge", "event_judge_escalate"]
    # 호출이 끝내 실패하면 None이다.
    call_id: UUID | None
    model_id: str | None = Field(max_length=200)
    prompt_version: str | None = Field(max_length=200)
    verification_status: VerificationStatus
    error_code: str | None = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")


class EventAnalyzed(_Execution):
    """이벤트 후보 하나의 판정. 후보마다 한 건. 발행: event-analyzer(SPEC-020 R-8~R-10)."""

    event_type: Literal["event.analyzed"] = "event.analyzed"
    schema_version: Literal[1] = 1
    candidate_id: UUID
    candidate_event_type: DetectableEventType
    start_ms: int = _MS
    end_ms: int = _MS
    evidence_frame_keys: list[Annotated[str, Field(pattern=_BLURRED_IMAGE)]] = Field(
        min_length=1, max_length=12
    )
    judgment_method: Literal["vector+vlm", "vector+vlm+escalate"]
    # 벡터 방식(지금은 전부)이면 있다. 탐지기 후보 방식이 생기면 그때 None이 된다.
    vector_candidate: VectorEvidence | None = Field(default=None, exclude_if=lambda v: v is None)
    analysis: VLMAnalysis
    judgments: list[Judgment] = Field(min_length=1, max_length=2)
    review_required: bool
    review_reasons: list[Literal["judgment_disagreement"]]
    taxonomy_version: Literal["v2"] = "v2"

    @model_validator(mode="after")
    def _vector_evidence(self) -> Self:
        if self.judgment_method.startswith("vector") and self.vector_candidate is None:
            raise ValueError("vectorCandidate is required for vector judgment methods")
        if len(set(self.review_reasons)) != len(self.review_reasons):
            raise ValueError("reviewReasons must be unique")
        return self


EVENT_MODELS: dict[str, type[Event]] = {
    "chunk.requested": ChunkRequested,
    "chunk.execution.finished": ChunkExecutionFinished,
    "video.uploaded": VideoUploaded,
    "video.deleted": VideoDeleted,
    "video.normalizing": VideoNormalizing,
    "video.normalized": VideoNormalized,
    "segment.ready": SegmentReady,
    "segment.embedded": SegmentEmbedded,
    "event.candidate.vector": EventCandidateVector,
    "event.analyzed": EventAnalyzed,
}
"""토픽 이름 → 이벤트 모델. 계약 테스트는 여기 등록된 모델을 모두 검증한다."""
