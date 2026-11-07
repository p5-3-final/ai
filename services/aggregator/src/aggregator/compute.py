"""job 하나의 순수 계산: 병합 → 상태(fold) → 규칙 → 노출 → 타임라인(R-6~R-17).

SPEC-028 재고 이벤트 중 confirmed는 병합 뒤 상태 계산에 더하고 타임라인에 `inventory_event`로
넣는다.

입력 목록 순서에 의존하지 않도록 각 단계가 정해진 키로 정렬한다(R-21). I/O가 없어 워커는
`asyncio.to_thread`로 부른다.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from vlm_contracts.aggregates import InventoryEvent, MergeGroup, Risk, StateSnapshot, TimelineItem
from vlm_contracts.events import RuleEvent

from aggregator.exposure import ExposureSpans, exposure_in
from aggregator.fold import RiskRecord, SessionState
from aggregator.merge import Merged, merge_events
from aggregator.model import AggregateInput, InputEvent
from aggregator.profile import LoadedProfile
from aggregator.rules import rule_events
from aggregator.state import confirmed_in_order, run_state
from aggregator.timeline import build_timeline


@dataclass(frozen=True, slots=True)
class Computed:
    """요약을 뺀 결과.

    Attributes:
        representatives: 병합 대표와 묶이지 않은 이벤트(타임라인 대상).
        confirmed: 상태 계산에 넣은 확정 대표.
        final_state: 영상 끝 상태. 세션 다음 영상·실시간 회전의 초기 상태가 된다.
    """

    representatives: tuple[InputEvent, ...]
    confirmed: tuple[InputEvent, ...]
    merge_groups: tuple[MergeGroup, ...]
    rule_events: tuple[RuleEvent, ...]
    snapshots: tuple[StateSnapshot, ...]
    risks: tuple[Risk, ...]
    timeline: tuple[TimelineItem, ...]
    final_state: SessionState


def compute(
    source: AggregateInput,
    loaded: LoadedProfile,
    *,
    merge: bool = True,
    inventory: Sequence[InventoryEvent] = (),
) -> Computed:
    """job의 결과를 계산한다. 같은 입력과 프로필이면 같은 값이다(R-21).

    Args:
        merge: 청크 경계 병합(R-6). 실시간 세션 마무리는 창마다 낸 상태와 같은 위험이 나오도록
            끈다(창 fold에는 병합이 없다).
        inventory: 재고 비교·장비 방치 이벤트(SPEC-028 R-9). confirmed만 상태 계산에 넣는다.
            청크 이벤트와 병합하지 않는다.
    """
    profile = loaded.profile
    merged = (
        merge_events(source.events, gap_ms=profile.merge_gap_ms, job_id=source.job_id)
        if merge
        else Merged(tuple(sorted(source.events, key=lambda e: (e.start_ms, e.event_id))), ())
    )
    stocked = [inventory_input(e) for e in inventory]
    confirmed = confirmed_in_order([*merged.representatives, *stocked])
    run = run_state(
        confirmed, source.initial_state, job_id=source.job_id, source_id=source.source_id
    )
    rules = rule_events(
        source.presences,
        source.tracks,
        source.zones,
        run.risks,
        loaded,
        job_id=source.job_id,
        end_ms=source.duration_ms,
    )
    spans = ExposureSpans.of(source.tracks, source.presences, profile.person_classes)
    risks = tuple(to_contract_risk(r, exposure_in(r, source.duration_ms, spans)) for r in run.risks)
    timeline = build_timeline(
        merged.representatives,
        rules,
        run.risks,
        [e for e in stocked if e.analysis.verification_status == "confirmed"],
    )
    return Computed(
        representatives=merged.representatives,
        confirmed=tuple(confirmed),
        merge_groups=merged.groups,
        rule_events=tuple(rules),
        snapshots=run.snapshots,
        risks=risks,
        timeline=tuple(timeline),
        final_state=run.final,
    )


def inventory_input(event: InventoryEvent) -> InputEvent:
    """재고 이벤트를 상태 계산 입력으로 바꾼다. 트랙·청크가 없고 판정 방식은 재고 이벤트 것이다."""
    return InputEvent(
        event_id=event.event_id,
        chunk_id=None,
        event_type=event.event_type,
        start_ms=event.start_ms,
        end_ms=event.end_ms,
        track_id=None,
        zone_id=event.zone_id,
        zone_name=None,
        judgment_method=event.judgment_method,
        score=None,
        analysis=event.analysis,
    )


def to_contract_risk(record: RiskRecord, exposure: object) -> Risk:
    """fold의 위험 값과 노출을 결과 파일 항목으로 바꾼다."""
    return Risk.model_validate(
        {
            "riskId": record.risk_id,
            "zoneId": record.zone_id,
            "riskType": record.risk_type,
            "status": record.status,
            "reason": record.reason,
            "firstSeenMs": record.first_seen_ms,
            "lastSeenMs": record.last_seen_ms,
            "resolvedAtMs": record.resolved_at_ms,
            "relatedEventIds": list(record.related_event_ids),
            "carriedOver": record.carried_over,
            "exposure": exposure,
        }
    )
