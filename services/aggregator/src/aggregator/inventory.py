"""정적 객체 재고 이어 맞추기와 변화 후보(SPEC-028 R-3~R-6, R-13, R-17).

청크 재고를 소유 범위 시작 순으로 놓고 이웃한 두 청크(세션이면 직전 영상의 끝 재고를 첫 청크 앞에)의
같은 클래스 객체를 박스 IoU가 큰 쌍부터 짝지어 사슬로 잇는다. 사슬이 끊긴 곳이 제거, 새로 시작한
곳이 등장이고, 같은 클래스의 제거·등장이 이동 창 안이면 이동이다. I/O가 없는 순수 함수라 워커는
`asyncio.to_thread`로 부른다. 입력 순서와 상관없이 같은 결과가 나오도록 모든 단계가 정해진 키로
정렬한다(R-17).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from uuid import UUID

from vlm_contracts.aggregates import (
    ChunkInventory,
    InventoryChange,
    InventoryChangeKind,
    InventoryObject,
)
from vlm_contracts.events import NormBox, VerificationStatus

from aggregator import ids
from aggregator.profile import AggregateProfile

_UNSET = UUID(int=0)


@dataclass(frozen=True, slots=True)
class Sighting:
    """사슬이 청크 하나에서 관측된 구간."""

    box: NormBox
    zone_id: UUID | None
    first_seen_ms: int
    last_seen_ms: int
    frame_key: str


@dataclass(frozen=True, slots=True)
class Chain:
    """같은 객체로 이은 사슬.

    Attributes:
        previous: 직전 영상 끝 재고에서 이어 왔으면 그 객체.
        sightings: 이 영상 청크에서의 관측. 직전 재고에서 시작해 첫 청크에 없으면 비어 있다.
    """

    object_id: str
    class_name: str
    previous: InventoryObject | None
    sightings: tuple[Sighting, ...]

    @property
    def first_seen_ms(self) -> int | None:
        return self.sightings[0].first_seen_ms if self.sightings else None

    @property
    def last_seen_ms(self) -> int | None:
        return self.sightings[-1].last_seen_ms if self.sightings else None


@dataclass(frozen=True, slots=True)
class Change:
    """변화 후보 하나(R-4~R-6). 판정 이미지(R-7)도 여기서 정한다.

    Attributes:
        before_frame: 직전 객체의 대표 프레임. 직전 영상 객체면 그 영상의 경로다.
        after_frame: 이후 시각에 가장 가까운 기준 프레임, `moved`·`appeared`면 이후 객체의 대표
            프레임.
    """

    change_id: UUID
    kind: InventoryChangeKind
    class_name: str
    zone_id: UUID | None
    before_ms: int | None
    after_ms: int | None
    before_object_id: str | None
    after_object_id: str | None
    before_frame: str | None
    after_frame: str | None

    def to_contract(
        self,
        *,
        event_id: UUID | None = None,
        call_id: UUID | None = None,
        status: VerificationStatus | None = None,
    ) -> InventoryChange:
        return InventoryChange(
            change_id=self.change_id,
            kind=self.kind,
            class_name=self.class_name,
            zone_id=self.zone_id,
            before_ms=self.before_ms,
            after_ms=self.after_ms,
            before_object_id=self.before_object_id,
            after_object_id=self.after_object_id,
            event_id=event_id,
            call_id=call_id,
            verification_status=status,
        )


@dataclass(frozen=True, slots=True)
class Comparison:
    """재고 비교 결과.

    Attributes:
        chains: 이 영상에서 관측된 사슬(objectId 순). 장비 방치(R-11) 입력이다.
        changes: `change_order` 순 변화 후보. 판정은 `judge_order` 순이다.
        end_inventory: 마지막 청크에 있는 사슬(objectId 순, R-13).
    """

    chains: tuple[Chain, ...]
    changes: tuple[Change, ...]
    end_inventory: tuple[InventoryObject, ...]


@dataclass(frozen=True, slots=True)
class _Node:
    """한 층(직전 재고 또는 청크 하나)의 객체. `key`는 짝짓기 동점을 정하는 ID다."""

    key: str
    class_name: str
    box: NormBox
    sighting: Sighting | None
    previous: InventoryObject | None = None


def iou(a: NormBox, b: NormBox) -> float:
    """두 박스의 IoU. 겹치지 않거나 넓이가 0이면 0이다."""
    w = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    h = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    shared = w * h
    union = a.w * a.h + b.w * b.h - shared
    return shared / union if union > 0 else 0.0


def compare(
    chunks: Sequence[ChunkInventory],
    previous: Sequence[InventoryObject],
    *,
    job_id: UUID,
    profile: AggregateProfile,
) -> Comparison:
    """청크 재고(와 직전 영상 끝 재고)를 이어 맞춰 사슬·변화 후보·끝 재고를 만든다.

    Args:
        chunks: job의 청크 재고. 순서는 상관없다(소유 범위 시작, chunkId 순으로 정렬한다).
        previous: 세션 직전 영상의 끝 재고(R-6). 청크가 없으면 비교하지 않는다.
    """
    ordered = sorted(chunks, key=lambda c: (c.owned_range.start_ms, str(c.chunk_id)))
    if not ordered:
        return Comparison((), (), ())
    builder = _ChainBuilder()
    if previous:
        layer = [
            _Node(o.object_id, o.class_name, o.box, None, o)
            for o in sorted(previous, key=lambda o: o.object_id)
        ]
        current = [builder.start(n, None) for n in layer]
        rest = ordered
    else:
        layer = _chunk_nodes(ordered[0])
        current = [builder.start(n, ordered[0].chunk_id) for n in layer]
        rest = ordered[1:]
    ended: list[tuple[int, int]] = []
    appeared: list[int] = []
    for chunk in rest:
        after = _chunk_nodes(chunk)
        pairs = dict(_match(layer, after, profile.inventory_iou))
        linked = {b: current[a] for a, b in pairs.items()}
        for b, chain in linked.items():
            builder.extend(chain, after[b])
        ended += [
            (current[a], chunk.owned_range.start_ms) for a in range(len(layer)) if a not in pairs
        ]
        nxt: list[int] = []
        for b, node in enumerate(after):
            if b in linked:
                nxt.append(linked[b])
            else:
                nxt.append(builder.start(node, chunk.chunk_id))
                appeared.append(nxt[-1])
        layer, current = after, nxt

    chains = builder.chains()
    frames = sorted((f.pts_ms, f.key) for c in ordered for f in c.reference_frames)
    removed = [_removed(chains[i], after_ms, frames) for i, after_ms in ended]
    changes = _changes(removed, [_appeared(chains[i]) for i in appeared], job_id, profile)
    end_inventory = sorted(
        (_inventory_object(chains[i]) for i in current if chains[i].sightings),
        key=lambda o: o.object_id,
    )
    observed = sorted((c for c in chains if c.sightings), key=lambda c: c.object_id)
    return Comparison(tuple(observed), changes, tuple(end_inventory))


class _ChainBuilder:
    """사슬을 만들며 모으는 지역 도우미. `chains()`로 불변 값을 낸다."""

    def __init__(self) -> None:
        self._heads: list[tuple[str, str, InventoryObject | None]] = []
        self._sightings: list[list[Sighting]] = []

    def start(self, node: _Node, chunk_id: UUID | None) -> int:
        if node.previous is not None:
            object_id = node.previous.object_id
        elif chunk_id is not None:
            object_id = ids.object_id(chunk_id, node.key)
        else:
            raise ValueError("a chunk object needs its chunkId")
        self._heads.append((object_id, node.class_name, node.previous))
        self._sightings.append([node.sighting] if node.sighting is not None else [])
        return len(self._heads) - 1

    def extend(self, chain: int, node: _Node) -> None:
        if node.sighting is not None:
            self._sightings[chain].append(node.sighting)

    def chains(self) -> list[Chain]:
        return [
            Chain(object_id, cls, prior, tuple(self._sightings[i]))
            for i, (object_id, cls, prior) in enumerate(self._heads)
        ]


def _chunk_nodes(chunk: ChunkInventory) -> list[_Node]:
    return [
        _Node(
            o.object_local_id,
            o.class_name,
            o.box,
            Sighting(o.box, o.zone_id, o.first_seen_ms, o.last_seen_ms, o.frame_key),
        )
        for o in sorted(chunk.static_objects, key=lambda o: o.object_local_id)
    ]


def _match(before: list[_Node], after: list[_Node], threshold: float) -> list[tuple[int, int]]:
    """같은 클래스·IoU 기준 이상 쌍을 (IoU 내림차순, 앞 키, 뒤 키) 순으로 하나씩 짝짓는다(R-3)."""
    by_class: dict[str, list[int]] = defaultdict(list)
    for b, node in enumerate(after):
        by_class[node.class_name].append(b)
    candidates: list[tuple[float, str, str, int, int]] = []
    for a, node in enumerate(before):
        for b in by_class.get(node.class_name, ()):
            overlap = iou(node.box, after[b].box)
            if overlap >= threshold:
                candidates.append((-overlap, node.key, after[b].key, a, b))
    return _greedy(candidates)


def _greedy(candidates: list[tuple[float, str, str, int, int]]) -> list[tuple[int, int]]:
    """정렬한 후보 쌍을 앞에서부터, 양쪽 다 아직 짝이 없을 때만 고른다."""
    used_a: set[int] = set()
    used_b: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for *_, a, b in sorted(candidates):
        if a in used_a or b in used_b:
            continue
        used_a.add(a)
        used_b.add(b)
        pairs.append((a, b))
    return pairs


def judge_order(c: Change) -> tuple[int, int, str]:
    """판정 순서(R-8): 직전 시각(없으면 앞), 이후 시각, changeId."""
    return (
        -1 if c.before_ms is None else c.before_ms,
        -1 if c.after_ms is None else c.after_ms,
        str(c.change_id),
    )


def change_order(c: Change) -> tuple[int, int, str]:
    """결과 순서: 변화가 시작된 시각(직전 시각, 없으면 이후 시각), 이후 시각, changeId."""
    at = c.before_ms if c.before_ms is not None else c.after_ms
    return (
        -1 if at is None else at,
        -1 if c.after_ms is None else c.after_ms,
        str(c.change_id),
    )


def _changes(
    removed: list[Change], arrivals: list[Change], job_id: UUID, profile: AggregateProfile
) -> tuple[Change, ...]:
    moved, removed, arrivals = _merge_moves(removed, arrivals, profile.move_window_ms)
    barriers = set(profile.barrier_classes)
    # 직전 영상 객체(직전 시각 없음)의 방호 클래스 제거만 미복구다. 이 영상 안에서 사라진 방호벽은
    # removed로 둔다(R-6, plan 4절).
    removed = [
        replace(r, kind="not_restored") if r.before_ms is None and r.class_name in barriers else r
        for r in removed
    ]
    made = [
        replace(
            c,
            change_id=ids.change_id(job_id, c.kind, c.before_object_id, c.after_object_id),
        )
        for c in (*removed, *arrivals, *moved)
    ]
    return tuple(sorted(made, key=change_order))


def _removed(chain: Chain, after_ms: int, frames: Sequence[tuple[int, str]]) -> Change:
    """사슬이 `after_ms`에 시작한 청크에서 끊겼다. 직전 영상 객체면 직전 시각이 없다(R-6)."""
    if chain.sightings:
        last = chain.sightings[-1]
        before_ms: int | None = last.last_seen_ms
        zone, frame = last.zone_id, last.frame_key
    elif chain.previous is not None:
        before_ms, zone, frame = None, chain.previous.zone_id, chain.previous.frame_key
    else:
        raise ValueError("a removed chain has neither sightings nor a previous object")
    return Change(
        change_id=_UNSET,
        kind="removed",
        class_name=chain.class_name,
        zone_id=zone,
        before_ms=before_ms,
        after_ms=after_ms,
        before_object_id=chain.object_id,
        after_object_id=None,
        before_frame=frame,
        after_frame=nearest_frame(frames, after_ms),
    )


def _appeared(chain: Chain) -> Change:
    first = chain.sightings[0]
    return Change(
        change_id=_UNSET,
        kind="appeared",
        class_name=chain.class_name,
        zone_id=first.zone_id,
        before_ms=None,
        after_ms=first.first_seen_ms,
        before_object_id=None,
        after_object_id=chain.object_id,
        before_frame=None,
        after_frame=first.frame_key,
    )


def _merge_moves(
    removed: list[Change], arrivals: list[Change], window_ms: int
) -> tuple[list[Change], list[Change], list[Change]]:
    """같은 클래스 제거·등장을 시간이 가까운 쌍부터 하나씩 `moved`로 합친다(R-5).

    등장이 제거보다 늦은 쌍만 본다. 직전 영상 객체의 제거는 직전 시각이 없어 첫 청크 시작(이후
    시각)을 기준으로 잰다.
    """
    candidates: list[tuple[float, str, str, int, int]] = []
    for r_index, r in enumerate(removed):
        at = r.before_ms if r.before_ms is not None else r.after_ms
        for a_index, a in enumerate(arrivals):
            if a.class_name != r.class_name or at is None or a.after_ms is None:
                continue
            gap = a.after_ms - at
            if 0 <= gap <= window_ms:
                candidates.append(
                    (gap, str(r.before_object_id), str(a.after_object_id), r_index, a_index)
                )
    pairs = _greedy(candidates)
    moved = [
        replace(
            removed[r_index],
            kind="moved",
            after_ms=arrivals[a_index].after_ms,
            after_object_id=arrivals[a_index].after_object_id,
            after_frame=arrivals[a_index].after_frame,
        )
        for r_index, a_index in pairs
    ]
    used_r = {r for r, _ in pairs}
    used_a = {a for _, a in pairs}
    rest_r = [r for i, r in enumerate(removed) if i not in used_r]
    rest_a = [a for i, a in enumerate(arrivals) if i not in used_a]
    return moved, rest_r, rest_a


def nearest_frame(frames: Sequence[tuple[int, str]], at_ms: int) -> str | None:
    """`at_ms`에 가장 가까운 기준 프레임. 거리가 같으면 이른 시각, 키 순이다."""
    if not frames:
        return None
    return min(frames, key=lambda f: (abs(f[0] - at_ms), f[0], f[1]))[1]


def _inventory_object(chain: Chain) -> InventoryObject:
    first, last = chain.sightings[0], chain.sightings[-1]
    return InventoryObject(
        object_id=chain.object_id,
        class_name=chain.class_name,
        box=last.box,
        zone_id=last.zone_id,
        first_seen_ms=first.first_seen_ms,
        last_seen_ms=last.last_seen_ms,
        frame_key=last.frame_key,
    )
