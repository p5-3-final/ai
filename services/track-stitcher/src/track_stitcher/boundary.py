"""경계 연결(SPEC-022 R-3, R-4, R-6). 입력 파일만 받는 순수 함수라 순서·Pod와 상관없이 같다."""

from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from vlm_contracts.events import TimeRange
from vlm_contracts.tracks import BoundaryBox, ChunkTrack, ChunkTracks

from track_stitcher.model import LocalTrack, TrackRef
from track_stitcher.profile import BoundaryProfile


class Owned(Protocol):
    """소유 범위가 있는 청크(트랙 요약 파일이나 상태 저장소의 청크 기록)."""

    @property
    def chunk_id(self) -> UUID: ...
    @property
    def owned_range(self) -> TimeRange: ...


@dataclass(frozen=True, slots=True)
class BoundaryLink:
    """경계 한 쌍에서 이은 두 로컬 트랙. `score`는 공통 관측 IoU 평균이다."""

    earlier: LocalTrack
    later: LocalTrack
    score: float


@dataclass(frozen=True, slots=True)
class PairResult:
    """경계 한 쌍의 연결 결과. `compared`는 비교한 같은 클래스 트랙 쌍 수다(R-21)."""

    links: tuple[BoundaryLink, ...]
    compared: int


def adjacent_pairs[C: Owned](chunks: Iterable[C]) -> list[tuple[C, C]]:
    """앞 청크 `ownedRange.endMs`와 뒤 청크 `ownedRange.startMs`가 같은 쌍(R-3), 시간순."""
    ordered = sorted(chunks, key=lambda c: (c.owned_range.start_ms, str(c.chunk_id)))
    by_start: dict[int, list[C]] = {}
    for chunk in ordered:
        by_start.setdefault(chunk.owned_range.start_ms, []).append(chunk)
    return [(a, b) for a in ordered for b in by_start.get(a.owned_range.end_ms, [])]


def overlap_window(earlier: ChunkTracks, later: ChunkTracks) -> tuple[int, int]:
    """두 청크가 모두 추적한 시간 `[뒤 처리 시작, 앞 처리 끝)`."""
    return later.processing_range.start_ms, earlier.processing_range.end_ms


def common_observations(
    earlier: Sequence[BoundaryBox],
    later: Sequence[BoundaryBox],
    window: tuple[int, int],
    tolerance_ms: int,
) -> list[tuple[BoundaryBox, BoundaryBox]]:
    """두 트랙의 공통 관측(R-6): 창 안에서 시각 차가 허용 오차 이하인 박스 짝.

    앞 트랙 박스를 시간순으로 돌며 아직 짝이 없는 뒤 박스 중 시각 차가 가장 작은 것(같으면 이른
    것)과 짝짓는다. 박스 하나는 한 짝에만 쓴다.
    """
    start, end = window
    first = sorted((b for b in earlier if start <= b.pts_ms < end), key=lambda b: b.pts_ms)
    second = sorted((b for b in later if start <= b.pts_ms < end), key=lambda b: b.pts_ms)
    times = [b.pts_ms for b in second]
    used: set[int] = set()
    pairs: list[tuple[BoundaryBox, BoundaryBox]] = []
    for box in first:
        lo = bisect_left(times, box.pts_ms - tolerance_ms)
        hi = bisect_right(times, box.pts_ms + tolerance_ms)
        free = [i for i in range(lo, hi) if i not in used]
        if not free:
            continue
        best = min(free, key=lambda i: (abs(times[i] - box.pts_ms), times[i]))
        used.add(best)
        pairs.append((box, second[best]))
    return pairs


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """`[x1, y1, x2, y2]` 두 박스의 IoU."""
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union


def link_pair(earlier: ChunkTracks, later: ChunkTracks, profile: BoundaryProfile) -> PairResult:
    """인접 청크 한 쌍의 경계 트랙을 일대일로 잇는다(R-4).

    후보는 앞 청크 `touchesTail` 트랙과 뒤 청크 `touchesHead` 트랙 중 클래스가 같은 쌍이다.
    공통 관측 IoU 평균과 공통 관측 수가 프로필 기준 이상인 쌍을 (IoU 내림차순, 앞 ID, 뒤 ID)
    순서로 보며 양쪽이 아직 비어 있으면 잇는다. 정렬 키가 완전해 결과가 결정적이다.
    """
    window = overlap_window(earlier, later)
    tails = [t for t in earlier.tracks if t.touches_tail]
    heads = [t for t in later.tracks if t.touches_head]
    compared = 0
    candidates: list[tuple[float, ChunkTrack, ChunkTrack]] = []
    for tail in tails:
        for head in heads:
            if tail.class_name != head.class_name:
                continue
            compared += 1
            pairs = common_observations(
                tail.boundary_boxes, head.boundary_boxes, window, profile.time_tolerance_ms
            )
            if len(pairs) < profile.min_common_observations:
                continue
            # 부동소수 오차로 1을 넘지 않게 자른다(계약 score 범위 -1~1).
            score = min(1.0, sum(iou(a.box, b.box) for a, b in pairs) / len(pairs))
            if score >= profile.min_mean_iou:
                candidates.append((score, tail, head))
    candidates.sort(key=lambda c: (-c[0], c[1].track_local_id, c[2].track_local_id))
    taken_tail: set[str] = set()
    taken_head: set[str] = set()
    links: list[BoundaryLink] = []
    for score, tail, head in candidates:
        if tail.track_local_id in taken_tail or head.track_local_id in taken_head:
            continue
        taken_tail.add(tail.track_local_id)
        taken_head.add(head.track_local_id)
        links.append(BoundaryLink(_local(earlier, tail), _local(later, head), score))
    return PairResult(tuple(links), compared)


def _local(file: ChunkTracks, track: ChunkTrack) -> LocalTrack:
    return LocalTrack(
        ref=TrackRef(file.chunk_id, file.execution_id, track.track_local_id),
        class_name=track.class_name,
        start_ms=track.start_ms,
        end_ms=track.end_ms,
    )
