"""이벤트 Pydantic 모델.

원본은 docs 레포 `service-contracts/events/*.schema.json`이고, 계약 테스트가 둘이 같은지 확인한다.
JSON 필드 이름은 camelCase, 파이썬 속성은 snake_case다.
"""

from typing import Literal, Self
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


class VideoDeleted(Event):
    """영상이 삭제(DELETED)로 바뀌었다. video.uploaded보다 먼저 올 수 있다. 발행: media-service."""

    event_type: Literal["video.deleted"] = "video.deleted"
    schema_version: Literal[1] = 1


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


EVENT_MODELS: dict[str, type[Event]] = {
    "chunk.requested": ChunkRequested,
    "chunk.execution.finished": ChunkExecutionFinished,
    "video.uploaded": VideoUploaded,
    "video.deleted": VideoDeleted,
    "video.normalizing": VideoNormalizing,
    "video.normalized": VideoNormalized,
}
"""토픽 이름 → 이벤트 모델. 계약 테스트는 여기 등록된 모델을 모두 검증한다."""
