"""frame-encoder 벡터 파일 Pydantic 모델(SPEC-019 R-3).

원본은 docs 레포 `service-contracts/vectors/v1/vector-file.schema.json`이고, 계약 테스트가
둘이 같은지 확인한다. JSON 필드 이름은 camelCase다.
"""

from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

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
