"""job 하나의 순수 계산: 병합 → 상태(fold) → 규칙 → 노출 → 타임라인(R-6~R-17).

입력 목록 순서에 의존하지 않도록 각 단계가 정해진 키로 정렬한다(R-21). I/O가 없어 워커는
`asyncio.to_thread`로 부른다.
"""

from dataclasses import dataclass

from vlm_contracts.aggregates import MergeGroup, Risk, StateSnapshot, TimelineItem
from vlm_contracts.events import RuleEvent

from aggregator.exposure import exposure_of
from aggregator.fold import RiskRecord, SessionState
from aggregator.merge import merge_events
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


def compute(source: AggregateInput, loaded: LoadedProfile) -> Computed:
    """업로드 job의 결과를 계산한다. 같은 입력과 프로필이면 같은 값이다(R-21)."""
    profile = loaded.profile
    merged = merge_events(source.events, gap_ms=profile.merge_gap_ms, job_id=source.job_id)
    confirmed = confirmed_in_order(merged.representatives)
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
    risks = tuple(
        to_contract_risk(
            r,
            exposure_of(
                r, source.duration_ms, source.tracks, source.presences, profile.person_classes
            ),
        )
        for r in run.risks
    )
    timeline = build_timeline(merged.representatives, rules, run.risks)
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
