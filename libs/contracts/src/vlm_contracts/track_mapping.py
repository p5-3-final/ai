"""트랙 번역표(`tracks/v1/track-mapping`) 읽기 모델(SPEC-025 R-7).

원본은 docs 레포 `service-contracts/tracks/v1/track-mapping.schema.json`이고 track-stitcher
(SPEC-022)가 쓴다. embedding-worker는 최종 trackId의 클래스·시각만 읽으므로 그 필드만 두고 나머지
(`members`, `links` 등)는 무시한다. 쓰는 쪽 전체 모델은 SPEC-022의 `vlm_contracts.tracks`다.
"""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from vlm_contracts.models import ClassName

_MS = Field(ge=0, le=9223372036854775807)


class _Summary(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="ignore"
    )


class MappedTrackSummary(_Summary):
    """번역표의 최종 트랙 하나에서 읽는 값."""

    track_id: UUID
    class_name: ClassName
    start_ms: int = _MS
    end_ms: int = _MS


class TrackMappingSummary(_Summary):
    """Job 하나의 번역표에서 읽는 값."""

    schema_version: Literal[1]
    source_id: UUID
    job_id: UUID
    tracks: list[MappedTrackSummary] = Field(max_length=200000)
