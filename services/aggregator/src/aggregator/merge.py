"""청크 경계 이벤트 병합(R-6, R-7).

한 청크 실행 안의 병합은 event-analyzer가 했다(SPEC-023). 여기서는 청크가 다른 이벤트만 잇는다.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.aggregates import MergeGroup

from aggregator import ids
from aggregator.model import InputEvent


@dataclass(frozen=True, slots=True)
class Merged:
    """병합 결과.

    Attributes:
        representatives: 묶음 대표와 묶이지 않은 이벤트. (startMs, eventId) 순이다.
        groups: 크기 2 이상인 묶음. 대표 eventId 순이다.
    """

    representatives: tuple[InputEvent, ...]
    groups: tuple[MergeGroup, ...]


def merge_events(events: Sequence[InputEvent], *, gap_ms: int, job_id: UUID) -> Merged:
    """같은 유형·같은 트랙(둘 다 없으면 같은 구역)·다른 청크이고 간격이 `gap_ms` 이하인 이벤트를
    한 묶음으로 잇는다(R-6). 이어진 이벤트는 모두 같은 묶음이다(합집합 찾기).
    """
    ordered = sorted(events, key=lambda e: (e.start_ms, e.event_id))
    parent = list(range(len(ordered)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for members in _by_key(ordered).values():
        _link(ordered, members, gap_ms, parent, find)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(ordered)):
        groups[find(i)].append(i)
    kept: list[InputEvent] = []
    merge_groups: list[MergeGroup] = []
    for members in groups.values():
        cluster = [ordered[i] for i in members]
        best = min(cluster, key=_rank)
        kept.append(best)
        if len(cluster) > 1:
            merge_groups.append(_group(job_id, best, cluster))
    kept.sort(key=lambda e: (e.start_ms, e.event_id))
    merge_groups.sort(key=lambda g: g.kept_event_id)
    return Merged(tuple(kept), tuple(merge_groups))


def _by_key(ordered: Sequence[InputEvent]) -> dict[tuple[str, str], list[int]]:
    keys: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, e in enumerate(ordered):
        # 청크가 없는 이벤트는 경계가 없어 병합 대상이 아니다(plan "spec이 정하지 않은 것").
        if e.chunk_id is None:
            continue
        if e.track_id is not None:
            keys[(e.event_type, f"track:{e.track_id}")].append(i)
        elif e.zone_id is not None:
            keys[(e.event_type, f"zone:{e.zone_id}")].append(i)
    return keys


def _link(
    ordered: Sequence[InputEvent],
    members: list[int],
    gap_ms: int,
    parent: list[int],
    find: Callable[[int], int],
) -> None:
    # 시작 시각 순이라, 앞 이벤트의 시작이 (지금 시작 - 간격 - 가장 긴 길이)보다 이르면
    # 더 볼 필요가 없다.
    longest = max(ordered[i].end_ms - ordered[i].start_ms for i in members)
    for pos, j in enumerate(members):
        later = ordered[j]
        for i in reversed(members[:pos]):
            earlier = ordered[i]
            if earlier.start_ms < later.start_ms - gap_ms - longest:
                break
            if earlier.chunk_id == later.chunk_id:
                continue
            if later.start_ms - earlier.end_ms <= gap_ms:
                parent[find(j)] = find(i)


def _rank(e: InputEvent) -> tuple[float, int, UUID]:
    # 점수 내림차순(null은 가장 낮음) → startMs → eventId(R-7)
    score = e.score if e.score is not None else float("-inf")
    return (-score, e.start_ms, e.event_id)


def _group(job_id: UUID, best: InputEvent, cluster: Sequence[InputEvent]) -> MergeGroup:
    return MergeGroup(
        group_id=ids.merge_group_id(job_id, best.event_id),
        kept_event_id=best.event_id,
        merged_event_ids=sorted(e.event_id for e in cluster if e is not best),
        event_type=best.event_type,
        start_ms=min(e.start_ms for e in cluster),
        end_ms=max(e.end_ms for e in cluster),
    )
