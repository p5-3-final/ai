"""aggregator 결과 파일 Pydantic 모델(SPEC-027 R-20).

원본은 docs 레포 `service-contracts/aggregates/v1/source-result.schema.json`이고 계약 테스트가 둘이
같은지 확인한다. 경로는 `sources/<sourceId>/jobs/<jobId>/aggregate/source-result.json`이다.
SPEC-028의 재고·예측 필드는 그 spec이 더한다.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from vlm_contracts.events import (
    EventType,
    RuleEvent,
    Severity,
    SourceSessionRef,
    SummaryStatus,
)

_MS = Field(ge=0, le=9223372036854775807)
_ZONE_REF = r"^(global|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
type ZoneRef = Annotated[str, Field(pattern=_ZONE_REF)]
"""구역 ID 문자열. 구역이 없는 영상·구역 밖 이벤트는 `global`이다."""

GLOBAL_ZONE = "global"


class _Model(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class ZoneState(_Model):
    zone_id: ZoneRef
    state: str = Field(min_length=1, max_length=500)


class CarriedRisk(_Model):
    """세션 직전 영상에서 끝까지 활성이던 위험(`initialState.activeRisks`)."""

    risk_id: UUID
    zone_id: ZoneRef
    risk_type: str = Field(min_length=1, max_length=100)
    reason: str = Field(max_length=1000)
    first_seen_ms: int = _MS


class InitialState(_Model):
    """상태 계산의 시작 상태. 세션 직전 영상의 마지막 상태이고 없으면 빈 목록이다(R-5)."""

    from_source_id: UUID | None
    zone_states: list[ZoneState]
    active_risks: list[CarriedRisk]


class MergeGroup(_Model):
    """청크 경계를 넘어 같은 사건으로 합친 이벤트 묶음(R-6, R-7)."""

    group_id: UUID
    kept_event_id: UUID
    merged_event_ids: list[UUID] = Field(min_length=1)
    event_type: EventType
    start_ms: int = _MS
    end_ms: int = _MS

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(set(self.merged_event_ids)) != len(self.merged_event_ids):
            raise ValueError("mergedEventIds must be unique")
        return self


class StateSnapshot(_Model):
    """구역 상태 스냅샷(R-12)."""

    snapshot_id: UUID
    zone_id: ZoneRef
    timestamp_ms: int = _MS
    previous_state: str | None = Field(max_length=500)
    current_state: str | None = Field(max_length=500)
    related_event_id: UUID


class Exposure(_Model):
    """위험 하나의 노출 지표(R-16)."""

    active_seconds: float = Field(ge=0)
    exposure_count: int = Field(ge=0)
    exposure_seconds: float = Field(ge=0)


class Risk(_Model):
    """위험 상태와 노출 지표(R-13~R-16)."""

    risk_id: UUID
    zone_id: ZoneRef
    risk_type: str = Field(min_length=1, max_length=100)
    status: Literal["active", "resolved"]
    reason: str = Field(max_length=1000)
    first_seen_ms: int = _MS
    last_seen_ms: int = _MS
    resolved_at_ms: int | None = Field(ge=0, le=9223372036854775807)
    related_event_ids: list[UUID]
    carried_over: bool
    exposure: Exposure


TimelineKind = Literal["event", "rule_event", "risk_activated", "risk_resolved", "inventory_event"]


class TimelineItem(_Model):
    """타임라인 항목(R-17). 같은 시각이면 refId 순이다."""

    at_ms: int = _MS
    kind: TimelineKind
    ref_id: UUID
    event_type: EventType | None
    zone_id: ZoneRef | None
    severity: Severity | None


class Highlight(_Model):
    at_ms: int = _MS
    text: str = Field(min_length=1, max_length=200)


class Summary(_Model):
    """`source_summary` 결과와 상태(R-18, R-19)."""

    status: SummaryStatus
    text: str | None = Field(max_length=1000)
    highlights: list[Highlight] = Field(max_length=5)
    call_id: UUID | None
    model_id: str | None
    prompt_version: str | None = Field(pattern=r"^[a-z_]+@[0-9a-f]{12}$")


class SourceResult(_Model):
    """영상 단위 결과 파일."""

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    normalization_id: UUID
    duration_ms: int = Field(ge=1, le=9223372036854775807)
    aggregate_profile: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    session: SourceSessionRef | None
    initial_state: InitialState
    merge_groups: list[MergeGroup]
    rule_events: list[RuleEvent]
    snapshots: list[StateSnapshot]
    risks: list[Risk]
    timeline: list[TimelineItem]
    summary: Summary


def result_key(source_id: UUID, job_id: UUID) -> str:
    """결과 파일 경로(R-20)."""
    return f"sources/{source_id}/jobs/{job_id}/aggregate/source-result.json"
