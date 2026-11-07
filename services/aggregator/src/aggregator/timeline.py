"""타임라인(R-17)."""

from collections.abc import Sequence

from vlm_contracts.aggregates import TimelineItem, TimelineKind
from vlm_contracts.events import EventType, RuleEvent

from aggregator.fold import RiskRecord
from aggregator.model import InputEvent


def build_timeline(
    representatives: Sequence[InputEvent],
    rule_events: Sequence[RuleEvent],
    risks: Sequence[RiskRecord],
    inventory: Sequence[InputEvent] = (),
) -> list[TimelineItem]:
    """병합 대표 이벤트(판정 상태와 상관없이), 규칙 이벤트, 위험 활성·해소를 (atMs, refId) 순으로
    놓는다. 이어 온 위험은 이 영상에서 활성된 것이 아니라 활성 항목을 넣지 않는다.

    `inventory`는 confirmed 재고 이벤트이고 종류 `inventory_event`로 넣는다(SPEC-028 R-9).

    위험 항목의 유형은 그 전이를 만든 이벤트의 유형이다(riskType은 자유 문장이라 유형이 아니다).
    """
    type_of: dict[object, EventType] = {
        e.event_id: e.event_type for e in (*representatives, *inventory)
    }
    kinds: tuple[tuple[TimelineKind, Sequence[InputEvent]], ...] = (
        ("event", representatives),
        ("inventory_event", inventory),
    )
    items = [
        TimelineItem(
            at_ms=e.start_ms,
            kind=kind,
            ref_id=e.event_id,
            event_type=e.event_type,
            zone_id=str(e.zone_id) if e.zone_id is not None else None,
            severity=e.analysis.severity,
        )
        for kind, group in kinds
        for e in group
    ]
    items += [
        TimelineItem(
            at_ms=r.start_ms,
            kind="rule_event",
            ref_id=r.event_id,
            event_type=r.event_type,
            zone_id=str(r.zone_id),
            severity=r.severity,
        )
        for r in rule_events
    ]
    for risk in risks:
        if not risk.carried_over:
            items.append(_risk_item(risk, "risk_activated", risk.first_seen_ms, type_of, 0))
        if risk.resolved_at_ms is not None:
            items.append(_risk_item(risk, "risk_resolved", risk.resolved_at_ms, type_of, -1))
    return sorted(items, key=lambda t: (t.at_ms, str(t.ref_id)))


def _risk_item(
    risk: RiskRecord,
    kind: str,
    at_ms: int,
    type_of: dict[object, EventType],
    which: int,
) -> TimelineItem:
    related = risk.related_event_ids
    event_type = type_of.get(related[which]) if related else None
    return TimelineItem.model_validate(
        {
            "atMs": at_ms,
            "kind": kind,
            "refId": risk.risk_id,
            "eventType": event_type,
            "zoneId": risk.zone_id,
            "severity": None,
        }
    )
