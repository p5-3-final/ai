"""장비 방치 규칙(SPEC-028 R-11).

정적 객체 사슬은 청크마다 움직이지 않은 객체를 이은 것이라, 사슬의 관측 구간이 곧 정지한 시간이다.
장비 클래스 사슬이 기준 시간 이상이고 그동안 같은 구역에 사람 트랙이 머물지 않았으면 방치로 본다.
판정(R-12)은 `judge.py`가 한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from aggregator import ids
from aggregator.exposure import overlap
from aggregator.inventory import Chain
from aggregator.model import Presence, TrackSpan
from aggregator.profile import AggregateProfile


@dataclass(frozen=True, slots=True)
class Abandoned:
    """장비 방치 후보 하나. 구간은 사슬의 처음 관측 ~ 마지막 관측이다."""

    event_id: UUID
    object_id: str
    class_name: str
    zone_id: UUID
    start_ms: int
    end_ms: int
    frame_key: str


def abandoned_equipment(
    chains: Sequence[Chain],
    tracks: Sequence[TrackSpan],
    presences: Sequence[Presence],
    profile: AggregateProfile,
    *,
    job_id: UUID,
) -> tuple[Abandoned, ...]:
    """장비 방치 후보를 (시작 시각, objectId) 순으로 낸다. 구역이 없는 객체는 대상이 아니다."""
    equipment = set(profile.equipment_classes)
    people = {t.track_id for t in tracks if t.class_name in profile.person_classes}
    stays = [p for p in presences if p.track_id in people]
    found: list[Abandoned] = []
    for chain in chains:
        if chain.class_name not in equipment or not chain.sightings:
            continue
        first, last = chain.sightings[0], chain.sightings[-1]
        zone = last.zone_id
        start, end = first.first_seen_ms, last.last_seen_ms
        if zone is None or end - start < profile.abandon_ms:
            continue
        if any(p.zone_id == zone and overlap(start, end, p.enter_ms, p.exit_ms) > 0 for p in stays):
            continue
        found.append(
            Abandoned(
                event_id=ids.equipment_event_id(job_id, chain.object_id),
                object_id=chain.object_id,
                class_name=chain.class_name,
                zone_id=zone,
                start_ms=start,
                end_ms=end,
                frame_key=last.frame_key,
            )
        )
    return tuple(sorted(found, key=lambda a: (a.start_ms, a.object_id)))
