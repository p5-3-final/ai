"""트랙 파일 Pydantic 모델(SPEC-022 R-1, R-12, R-13).

원본은 docs 레포 `service-contracts/tracks/v1/`의 JSON Schema이고, 계약 테스트가 둘이 같은지
확인한다. 트랙 요약(`chunk-tracks`)은 chunk-processor가, 번역표(`track-mapping`)는 track-stitcher가
쓴다. JSON 필드 이름은 camelCase다.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from vlm_contracts.events import TimeRange, TrackMember
from vlm_contracts.models import ClassName

_MS = Field(ge=0, le=9223372036854775807)
_TRACK_LOCAL_ID = r"^[A-Za-z0-9_-]{1,64}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$"
_UUID36 = r"[0-9a-f-]{36}"
_CROP_KEY = (
    rf"^sources/{_UUID36}/jobs/{_UUID36}/chunks/{_UUID36}/executions/{_UUID36}/"
    r"blurred/[^/?#]+\.(jpg|jpeg|png|webp)$"
)

type UnitCoordinate = Annotated[float, Field(ge=0, le=1)]


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class BoundaryBox(_Model):
    """경계 창 안의 박스 하나. 좌표는 프레임 크기로 나눈 0~1, 왼쪽 위가 원점이다."""

    pts_ms: int = _MS
    box: list[UnitCoordinate] = Field(min_length=4, max_length=4)
    confidence: float | None = Field(default=None, ge=0, le=1, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        # 계약 설명의 x1<x2, y1<y2. 넓이가 0인 박스는 IoU를 정의할 수 없다.
        x1, y1, x2, y2 = self.box
        if not (x1 < x2 and y1 < y2):
            raise ValueError("box must satisfy x1 < x2 and y1 < y2")
        return self


class RepresentativeCrop(_Model):
    """트랙 대표 크롭(블러 사본)."""

    crop_id: UUID
    pts_ms: int = _MS
    key: str = Field(pattern=_CROP_KEY)


class ChunkTrack(_Model):
    """청크 실행 하나의 로컬 트랙 요약."""

    track_local_id: str = Field(pattern=_TRACK_LOCAL_ID)
    class_name: ClassName
    start_ms: int = _MS
    end_ms: int = _MS
    touches_head: bool
    touches_tail: bool
    observation_count: int = Field(ge=1)
    boundary_boxes: list[BoundaryBox] = Field(max_length=512)
    # 크롭이 없는 트랙은 빈 배열이거나 없다. 받은 모양을 바꾸지 않도록 None이면 뺀다.
    representative_crops: list[RepresentativeCrop] | None = Field(
        default=None, max_length=3, exclude_if=lambda v: v is None
    )


class ChunkTracks(_Model):
    """실행 하나의 트랙 요약 파일(`tracks/v1/chunk-tracks`)."""

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    normalization_id: UUID
    owned_range: TimeRange
    processing_range: TimeRange
    detector_profile: str = Field(pattern=_VERSION)
    tracks: list[ChunkTrack] = Field(max_length=4096)

    @model_validator(mode="after")
    def _unique_tracks(self) -> Self:
        # 번역표에 로컬 트랙이 정확히 한 번 나오려면(SPEC-022 R-13) 실행 안에서 ID가 고유해야 한다.
        ids = [t.track_local_id for t in self.tracks]
        if len(set(ids)) != len(ids):
            raise ValueError("trackLocalId must be unique within the execution")
        return self


class MappedTrack(_Model):
    """번역표의 최종 트랙 하나."""

    track_id: UUID
    class_name: ClassName
    start_ms: int = _MS
    end_ms: int = _MS
    members: list[TrackMember] = Field(min_length=1)


LinkKind = Literal["boundary", "reid"]


class TrackLink(_Model):
    """로컬 트랙 사이 연결 하나와 그 근거."""

    kind: LinkKind
    from_: TrackMember = Field(alias="from")
    to: TrackMember
    score: float = Field(ge=-1, le=1)
    gap_ms: int | None = Field(
        default=None, ge=0, le=9223372036854775807, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _gap_for_reid(self) -> Self:
        if (self.kind == "reid") != (self.gap_ms is not None):
            raise ValueError("gapMs is required for reid links and not allowed for boundary links")
        return self


class TrackMapping(_Model):
    """Job 하나의 로컬 트랙 → 최종 트랙 번역표(`tracks/v1/track-mapping`)."""

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    normalization_id: UUID
    stitch_profile: str = Field(pattern=_VERSION)
    # Re-ID를 하지 않았으면 None(JSON null). 계약이 null을 값으로 요구하므로 빼지 않는다.
    embedding_model: str | None = Field(pattern=r"^[^:\s]+:[^:\s]+:[^:\s]+:[^:\s]+$")
    tracks: list[MappedTrack] = Field(max_length=200000)
    links: list[TrackLink] = Field(max_length=200000)
