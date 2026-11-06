"""위험구역 체류·배회 규칙 이벤트(R-8~R-10).

chunk-processor는 트랙별 구역 진입·이탈 시각만 남긴다. 시간이 기준인 규칙은 영상 전체가 모인 뒤에만
셀 수 있어 여기서 만든다. 규칙 이벤트는 상태와 위험을 바꾸지 않는다(R-10).
"""

from collections import defaultdict
from collections.abc import Sequence
from uuid import UUID

from vlm_contracts.events import RuleEvent

from aggregator import ids
from aggregator.exposure import overlap
from aggregator.fold import RiskRecord
from aggregator.model import Presence, TrackSpan, ZoneInfo
from aggregator.profile import LoadedProfile

_HAZARD = "hazard"


def rule_events(
    presences: Sequence[Presence],
    tracks: Sequence[TrackSpan],
    zones: Sequence[ZoneInfo],
    risks: Sequence[RiskRecord],
    loaded: LoadedProfile,
    *,
    job_id: UUID,
    end_ms: int | None = None,
) -> list[RuleEvent]:
    """`hazard` 구역의 사람 트랙 머문 구간으로 dwell·loiter를 만들고 severity를 정한다.

    Args:
        risks: 상태 계산이 끝난 위험. 같은 구역 위험의 활성 구간과 겹치면 high다(R-10).
        end_ms: 해소되지 않은 위험의 활성 끝. 없으면 끝이 없는 것으로 본다.

    Returns:
        (startMs, eventId) 순 규칙 이벤트.
    """
    profile = loaded.profile
    hazards = {z.zone_id: z for z in zones if z.kind == _HAZARD}
    people = {t.track_id for t in tracks if t.class_name in profile.person_classes}
    stays = sorted(
        (p for p in presences if p.zone_id in hazards and p.track_id in people),
        key=lambda p: (p.enter_ms, p.track_id, p.zone_id),
    )
    horizon = end_ms if end_ms is not None else max((p.exit_ms for p in stays), default=0)
    drafts: list[tuple[str, Presence, int, str]] = []
    for p in stays:
        if p.exit_ms - p.enter_ms >= profile.dwell_ms:
            text = profile.dwell_description.format(
                zone=hazards[p.zone_id].name, seconds=profile.dwell_ms // 1000
            )
            drafts.append(("dwell", p, p.exit_ms, text))
    by_pair: dict[tuple[UUID, UUID], list[Presence]] = defaultdict(list)
    for p in stays:
        by_pair[(p.track_id, p.zone_id)].append(p)
    for pair_stays in by_pair.values():
        for first, last in _loiters(pair_stays, profile.loiter_window_ms, profile.loiter_count):
            text = profile.loiter_description.format(
                zone=hazards[first.zone_id].name,
                seconds=profile.loiter_window_ms // 1000,
                count=profile.loiter_count,
            )
            drafts.append(("loiter", first, last.exit_ms, text))
    made = [
        RuleEvent(
            event_id=ids.rule_event_id(job_id, rule, p.track_id, p.zone_id, p.enter_ms),
            rule=rule,
            track_id=p.track_id,
            zone_id=p.zone_id,
            start_ms=p.enter_ms,
            end_ms=end,
            description=text,
            severity="high" if _during_risk(p, end, risks, horizon) else "medium",
        )
        for rule, p, end, text in drafts
    ]
    return sorted(made, key=lambda r: (r.start_ms, r.event_id))


def _loiters(
    stays: Sequence[Presence], window_ms: int, count: int
) -> list[tuple[Presence, Presence]]:
    """창(`window_ms`) 안 진입이 `count`번이면 하나, 그 마지막 진입 뒤부터 다음 창을 센다(R-9)."""
    found: list[tuple[Presence, Presence]] = []
    i = 0
    while i + count - 1 < len(stays):
        first, last = stays[i], stays[i + count - 1]
        if last.enter_ms - first.enter_ms <= window_ms:
            found.append((first, last))
            i += count
        else:
            i += 1
    return found


def _during_risk(stay: Presence, end_ms: int, risks: Sequence[RiskRecord], horizon: int) -> bool:
    zone = str(stay.zone_id)
    return any(
        r.zone_id == zone
        and overlap(stay.enter_ms, end_ms, r.first_seen_ms, r.active_until(max(horizon, end_ms)))
        > 0
        for r in risks
    )
