"""결과 파일·`source.completed` 조립과 고정 직렬화(R-20, R-21, R-28)."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from vlm_contracts.aggregates import (
    Forecast,
    InventoryChange,
    InventoryEvent,
    InventoryObject,
    SourceResult,
    Summary,
    result_key,
)
from vlm_contracts.events import (
    AnalysisCompleted,
    MissingCounts,
    SessionRef,
    SourceCompleted,
    SourceCompletedCounts,
    SourceSessionRef,
    SummaryStatus,
)

from aggregator import ids
from aggregator.compute import Computed
from aggregator.model import AggregateInput


def empty_summary(status: SummaryStatus) -> Summary:
    """요약 문장이 없는 요약(`empty`·`not_configured`·`failed`)."""
    return Summary(
        status=status, text=None, highlights=[], call_id=None, model_id=None, prompt_version=None
    )


def source_session(session: SessionRef | None) -> SourceSessionRef | None:
    """`source.completed`·결과 파일의 세션.

    계약의 결과 쪽 세션은 실시간 회전(ROTATION)을 담지 않는다. 회전 세션은 null로 내고 직전 영상은
    `initialState.fromSourceId`에 남는다(계약 변경 필요 항목으로 보고).
    """
    if session is None or session.role == "ROTATION":
        return None
    return SourceSessionRef(
        session_id=session.session_id,
        role=session.role,
        previous_source_id=session.previous_source_id,
    )


@dataclass(frozen=True, slots=True)
class InventoryResult:
    """재고 단계 결과(SPEC-028 R-9, R-13). 재고 파일이 끝내 모자라면 만들지 않는다(R-2)."""

    inventory: tuple[InventoryObject, ...]
    changes: tuple[InventoryChange, ...]
    events: tuple[InventoryEvent, ...]


def build_result(
    source: AggregateInput,
    computed: Computed,
    summary: Summary,
    version: str,
    *,
    inventory: InventoryResult | None = None,
    forecasts: Sequence[Forecast] | None = None,
) -> SourceResult:
    """결과 파일. 목록은 계산 단계가 정한 순서 그대로다.

    재고·예측이 None이면 그 필드를 빼고 낸다(SPEC-028 선택 필드).
    """
    return SourceResult(
        source_id=source.source_id,
        job_id=source.job_id,
        normalization_id=source.normalization_id,
        duration_ms=source.duration_ms,
        aggregate_profile=version,
        session=source_session(source.session),
        initial_state=source.initial_state,
        merge_groups=list(computed.merge_groups),
        rule_events=list(computed.rule_events),
        snapshots=list(computed.snapshots),
        risks=list(computed.risks),
        timeline=list(computed.timeline),
        summary=summary,
        inventory=list(inventory.inventory) if inventory else None,
        inventory_changes=list(inventory.changes) if inventory else None,
        inventory_events=list(inventory.events) if inventory else None,
        forecasts=list(forecasts) if forecasts is not None else None,
    )


def encode_result(result: SourceResult) -> bytes:
    """고정 직렬화. 필드 순서는 모델 정의 순서, 실수는 계산에서 소수 셋째 자리로 끊었다(R-21)."""
    return result.model_dump_json(by_alias=True).encode()


class AggregateMeta(BaseModel):
    """결과 파일 옆에 두는 aggregator 자기 기록(R-27, R-28).

    계약의 결과 파일은 추가 필드를 허용하지 않아 입력 eventId 집합과 revision을 따로 둔다
    (계약 변경 필요 항목으로 보고). 경로는 결과 파일과 같은 `aggregate/` 폴더다.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )

    revision: int = Field(ge=1)
    input_event_ids: list[UUID]
    partial: bool = False
    # 늦은 벡터 이벤트로 다시 계산할 때(R-27) 같은 입력을 다시 읽으려고 받은 완료 메시지를 둔다.
    completed: AnalysisCompleted


def meta_key(source_id: UUID, job_id: UUID) -> str:
    return f"sources/{source_id}/jobs/{job_id}/aggregate/source-result.meta.json"


def source_completed(
    source: AggregateInput,
    computed: Computed,
    *,
    summary_status: SummaryStatus,
    version: str,
    revision: int,
    occurred_at: datetime,
    missing: MissingCounts | None = None,
    job_type: str | None = None,
    inventory: InventoryResult | None = None,
    forecasts: Sequence[Forecast] | None = None,
) -> SourceCompleted:
    """`source.completed`. eventId는 jobId·revision으로 결정적이다(R-20, R-28)."""
    counts = SourceCompletedCounts(
        input_events=len(source.events),
        confirmed_events=len(computed.confirmed),
        merge_groups=len(computed.merge_groups),
        rule_events=len(computed.rule_events),
        snapshots=len(computed.snapshots),
        risks=len(computed.risks),
        active_risks_at_end=len(computed.final_state.active_risks),
        inventory_events=len(inventory.events) if inventory else None,
        forecasts=len(forecasts) if forecasts is not None else None,
    )
    return SourceCompleted.model_validate(
        {
            "eventId": ids.source_completed_id(source.job_id, revision),
            "occurredAt": occurred_at,
            "sourceId": source.source_id,
            "ownerId": source.owner_id,
            "jobType": job_type,
            "jobId": source.job_id,
            "normalizationId": source.normalization_id,
            "revision": revision,
            "partial": True if missing is not None else None,
            "missingCounts": missing,
            "resultKey": result_key(source.source_id, source.job_id),
            "aggregateProfile": version,
            "session": source_session(source.session),
            "counts": counts,
            "summaryStatus": summary_status,
        }
    )


__all__ = [
    "AggregateMeta",
    "InventoryResult",
    "build_result",
    "empty_summary",
    "encode_result",
    "meta_key",
    "source_completed",
    "source_session",
]
