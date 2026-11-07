"""grounding 결과 파일 Pydantic 모델(SPEC-026 R-6, R-17).

원본은 docs 레포 `service-contracts/groundings/v1/grounding-result.schema.json`이고, 계약 테스트가
둘이 같은지 확인한다. JSON 필드 이름은 camelCase다.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from vlm_contracts.events import GROUNDING_MODEL_PATTERN, GroundingTarget

_MS = Field(ge=0, le=9223372036854775807)
_UNIT = Field(ge=0, le=1)


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class GroundingBox(_Model):
    """프레임 크기로 나눈 0~1 좌표. 왼쪽 위 기준."""

    x: float = _UNIT
    y: float = _UNIT
    w: float = _UNIT
    h: float = _UNIT


class GroundingDetection(_Model):
    """기준 신뢰도 이상인 탐지 하나."""

    target: GroundingTarget
    box: GroundingBox
    confidence: float = _UNIT


class GroundingResultFrame(_Model):
    """샘플 프레임 하나."""

    pts_ms: int = _MS
    detections: list[GroundingDetection] = Field(max_length=100)


class GroundingClip(_Model):
    """주석 클립 정보."""

    key: str = Field(
        pattern=r"^sources/[0-9a-f-]{36}/groundings/[0-9a-f-]{36}/[A-Za-z0-9._-]{1,100}/clip[.]mp4$"
    )
    codec: Literal["h264"] = "h264"
    fps: float = Field(gt=0, le=240)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    duration_ms: int = Field(ge=1)
    audio: bool


class GroundingResultFile(_Model):
    """주석 구간 하나의 탐지 결과 파일."""

    schema_version: Literal[1] = 1
    source_id: UUID
    grounding_id: UUID
    segment_id: UUID
    start_ms: int = _MS
    end_ms: int = _MS
    targets: list[GroundingTarget] = Field(min_length=1, max_length=3)
    grounding_model: str = Field(pattern=GROUNDING_MODEL_PATTERN)
    score_threshold: float = _UNIT
    sample_fps: float = Field(gt=0, le=5)
    colors: dict[str, Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]]
    clip: GroundingClip
    frames: list[GroundingResultFrame] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def _targets_unique(self) -> Self:
        if len(set(self.targets)) != len(self.targets):
            raise ValueError("targets must be unique")
        return self
