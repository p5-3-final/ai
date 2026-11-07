"""벡터 파일 Pydantic 모델.

frame-encoder 벡터 파일(SPEC-019 R-3)의 원본은 docs 레포
`service-contracts/vectors/v1/vector-file.schema.json`, embedding-worker 텍스트 벡터 파일
(SPEC-025 R-6)의 원본은 `vectors/v1/text-vector-file.schema.json`이고, 계약 테스트가 둘이 같은지
확인한다. JSON 필드 이름은 camelCase다.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from vlm_contracts.events import (
    TEXT_FORMAT_PATTERN,
    EventType,
    ItemKind,
    PpeState,
    Severity,
    VerificationStatus,
    WorkerRole,
)
from vlm_contracts.models import ClassName

_MS = Field(ge=0, le=9223372036854775807)


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class SegmentVector(_Model):
    """구간 벡터. 프레임 4장 벡터를 각각 정규화해 평균한 뒤 다시 정규화한 값이다."""

    segment_id: UUID
    start_ms: int = _MS
    end_ms: int = _MS
    static: bool
    vector: list[float] = Field(min_length=1, max_length=4096)


class CropVector(_Model):
    """트랙 대표 크롭 벡터."""

    crop_id: UUID
    track_local_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    pts_ms: int = _MS
    vector: list[float] = Field(min_length=1, max_length=4096)


class VectorFile(_Model):
    """실행 하나의 구간·크롭 벡터 파일."""

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    normalization_id: UUID
    embedding_model: str = Field(pattern=r"^[^:\s]+:[^:\s]+:[^:\s]+:[^:\s]+$")
    dimension: int = Field(ge=1, le=4096)
    segments: list[SegmentVector] = Field(max_length=64)
    crops: list[CropVector] = Field(max_length=512)

    @model_validator(mode="after")
    def _dimension_matches(self) -> Self:
        # 계약은 벡터 길이가 dimension과 같다고 정한다. 스키마로는 표현할 수 없어 여기서 확인한다.
        vectors = [s.vector for s in self.segments] + [c.vector for c in self.crops]
        if any(len(v) != self.dimension for v in vectors):
            raise ValueError("vector length must equal dimension")
        return self


def _omit_none(value: object) -> bool:
    return value is None


class TextVectorMetadata(_Model):
    """텍스트 항목의 검색 필터·이동 시각 값(SPEC-025 R-7).

    계약은 null을 받지 않는다. 해당 없는 필터는 None으로 두고 JSON에서 뺀다.
    """

    start_ms: int = _MS
    end_ms: int = _MS
    event_type: EventType | None = Field(default=None, exclude_if=_omit_none)
    severity: Severity | None = Field(default=None, exclude_if=_omit_none)
    verification_status: VerificationStatus | None = Field(default=None, exclude_if=_omit_none)
    track_id: UUID | None = Field(default=None, exclude_if=_omit_none)
    class_name: ClassName | None = Field(default=None, exclude_if=_omit_none)
    helmet: PpeState | None = Field(default=None, exclude_if=_omit_none)
    vest: PpeState | None = Field(default=None, exclude_if=_omit_none)
    role: WorkerRole | None = Field(default=None, exclude_if=_omit_none)
    segment_id: UUID | None = Field(default=None, exclude_if=_omit_none)

    @model_validator(mode="before")
    @classmethod
    def _no_null(cls, data: object) -> object:
        # 빠진 필드와 null을 구분한다. 계약은 빠진 것만 허용하므로 들어온 null은 거절한다.
        if isinstance(data, dict) and any(v is None for v in data.values()):
            raise ValueError("metadata values must not be null; omit the field instead")
        return data


class TextVectorFile(_Model):
    """텍스트 항목 하나의 벡터 파일(`vectors/v1/text-vector-file`)."""

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    item_kind: ItemKind
    item_id: UUID
    text: str = Field(min_length=1, max_length=2000)
    text_format: str = Field(pattern=TEXT_FORMAT_PATTERN)
    embedding_model: str = Field(min_length=1, max_length=200)
    dimension: int = Field(ge=1, le=4096)
    vector: list[float] = Field(min_length=1, max_length=4096)
    metadata: TextVectorMetadata

    @model_validator(mode="after")
    def _dimension_matches(self) -> Self:
        # 계약은 벡터 길이가 dimension과 같다고 정한다. 스키마로는 표현할 수 없어 여기서 확인한다.
        if len(self.vector) != self.dimension:
            raise ValueError("vector length must equal dimension")
        return self
