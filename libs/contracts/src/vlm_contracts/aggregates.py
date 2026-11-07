"""aggregator 결과 파일 Pydantic 모델(SPEC-027 R-20).

원본은 docs 레포 `service-contracts/aggregates/v1/source-result.schema.json`이고 계약 테스트가 둘이
같은지 확인한다. 경로는 `sources/<sourceId>/jobs/<jobId>/aggregate/source-result.json`이다.
SPEC-028의 재고·예측 필드(`inventory`·`inventoryChanges`·`inventoryEvents`·`forecasts`)는 선택
필드라 없으면 JSON에서 뺀다. 청크 재고 파일(`aggregates/v1/chunk-inventory`)도 여기 둔다.
"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from vlm_contracts.events import (
    BLURRED_IMAGE_PATTERN,
    EventType,
    NormBox,
    ReferenceFrame,
    RuleEvent,
    Severity,
    SourceSessionRef,
    StaticObject,
    SummaryStatus,
    TimeRange,
    VerificationStatus,
    VLMAnalysis,
)
from vlm_contracts.models import ClassName

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


type ImageKey = Annotated[str, Field(pattern=BLURRED_IMAGE_PATTERN)]
_NULLABLE_MS = Field(ge=0, le=9223372036854775807)


class InventoryObject(_Model):
    """영상 끝 재고의 정적 객체 하나(SPEC-028 R-13). 세션 다음 영상의 초기 재고가 된다(R-6)."""

    object_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    class_name: ClassName
    box: NormBox
    zone_id: UUID | None
    first_seen_ms: int = _MS
    last_seen_ms: int = _MS
    frame_key: ImageKey


InventoryChangeKind = Literal["removed", "appeared", "moved", "not_restored"]


class InventoryChange(_Model):
    """재고 비교 후보 하나와 판정(SPEC-028 R-4~R-8). 판정하지 않았으면 eventId·상태가 null이다."""

    change_id: UUID
    kind: InventoryChangeKind
    class_name: ClassName
    zone_id: UUID | None
    before_ms: int | None = _NULLABLE_MS
    after_ms: int | None = _NULLABLE_MS
    before_object_id: str | None
    after_object_id: str | None
    event_id: UUID | None
    call_id: UUID | None
    verification_status: VerificationStatus | None


InventoryEventType = Literal[
    "object_removed", "object_moved", "barrier_not_restored", "equipment_abandoned"
]
InventoryJudgment = Literal["inventory+state_compare", "rule", "rule+vlm"]


class InventoryEvent(_Model):
    """재고 비교·장비 방치가 만든 이벤트(SPEC-028 R-9~R-12). confirmed면 상태 계산에 들어간다."""

    event_id: UUID
    event_type: InventoryEventType
    start_ms: int = _MS
    end_ms: int = _MS
    zone_id: UUID | None
    judgment_method: InventoryJudgment
    evidence_frame_keys: list[ImageKey] = Field(max_length=4)
    analysis: VLMAnalysis
    call_id: UUID | None


class ForecastHorizon(_Model):
    horizon_sec: int = Field(ge=60, le=86400)
    exposure_probability: float = Field(ge=0, le=1)


type FeatureName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class RiskForecast(_Model):
    """ai-gateway `risk_forecast` 출력(`events/_risk-forecast`). 미설정 대체 결과는 horizons가 비어
    있다(SPEC-028 R-15)."""

    method: Literal["rule", "model"]
    horizons: list[ForecastHorizon] = Field(max_length=6)
    features: dict[FeatureName, float]


ForecastStatus = Literal["generated", "not_configured", "failed"]


class Forecast(_Model):
    """영상 끝 활성 위험 하나의 예측 결과(SPEC-028 R-14, R-15)."""

    risk_id: UUID
    status: ForecastStatus
    forecast: RiskForecast | None
    call_id: UUID | None


def _absent_if_none(value: object) -> bool:
    return value is None


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
    inventory: list[InventoryObject] | None = Field(default=None, exclude_if=_absent_if_none)
    inventory_changes: list[InventoryChange] | None = Field(
        default=None, exclude_if=_absent_if_none
    )
    inventory_events: list[InventoryEvent] | None = Field(default=None, exclude_if=_absent_if_none)
    forecasts: list[Forecast] | None = Field(default=None, exclude_if=_absent_if_none)


def result_key(source_id: UUID, job_id: UUID) -> str:
    """결과 파일 경로(R-20)."""
    return f"sources/{source_id}/jobs/{job_id}/aggregate/source-result.json"


class ChunkInventory(_Model):
    """청크 재고 파일(`aggregates/v1/chunk-inventory`, SPEC-028 R-1).

    청크마다 처음 받은 `chunk.manifest`의 재고를 옮겨 둔다. 처음 쓴 실행이 이긴다.
    """

    schema_version: Literal[1] = 1
    source_id: UUID
    job_id: UUID
    chunk_id: UUID
    execution_id: UUID
    owned_range: TimeRange
    detector_profile: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    static_objects: list[StaticObject] = Field(max_length=500)
    reference_frames: list[ReferenceFrame] = Field(min_length=1, max_length=8)


def inventory_prefix(source_id: UUID, job_id: UUID) -> str:
    """job의 청크 재고 파일 폴더(R-1, R-2)."""
    return f"sources/{source_id}/jobs/{job_id}/aggregate/inventory/"


def inventory_key(source_id: UUID, job_id: UUID, chunk_id: UUID) -> str:
    """청크 재고 파일 경로(R-1)."""
    return f"{inventory_prefix(source_id, job_id)}{chunk_id}.json"
