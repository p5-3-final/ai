"""재등장 연결(Re-ID, SPEC-022 R-9, R-10). 입력만 받는 순수 함수다.

벡터 계산은 표준 라이브러리(`math.sumprod`)로 한다. 후보를 최대 간격 창 안으로 줄여 1시간 영상도
NFR-1 안에 끝난다(`test_stitcher_scale.py`로 확인, D-6).
"""

import math
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from track_stitcher.model import LocalTrack, TrackRef
from track_stitcher.profile import ReidProfile


@dataclass(frozen=True, slots=True)
class Group:
    """경계 연결 뒤 트랙 하나. 구성원은 R-14 순서(시작 시각, chunkId, trackLocalId)다.

    Attributes:
        members: 경계 연결로 묶인 로컬 트랙.
        vector: 구성원 크롭 벡터 평균의 단위 벡터. 크롭 벡터가 없으면 None이다(R-10).
    """

    members: tuple[LocalTrack, ...]
    vector: tuple[float, ...] | None

    @classmethod
    def of(cls, members: Iterable[LocalTrack], vector: tuple[float, ...] | None) -> "Group":
        return cls(tuple(sorted(members, key=LocalTrack.order_key)), vector)

    @property
    def first(self) -> LocalTrack:
        return self.members[0]

    @property
    def last(self) -> LocalTrack:
        """끝 시각이 가장 늦은 구성원(같으면 R-14 순서로 앞선 것). Re-ID 근거의 `from`이다."""
        return min(self.members, key=lambda m: (-m.end_ms, m.order_key()))

    @property
    def class_name(self) -> str:
        return self.first.class_name

    @property
    def start_ms(self) -> int:
        return self.first.start_ms

    @property
    def end_ms(self) -> int:
        return self.last.end_ms


@dataclass(frozen=True, slots=True)
class ReidLink:
    """재등장으로 이은 두 트랙. `score`는 코사인 유사도, `gap_ms`는 앞 끝과 뒤 시작 사이다."""

    earlier: Group
    later: Group
    score: float
    gap_ms: int


@dataclass(frozen=True, slots=True)
class ReidResult:
    """Re-ID 결과. `compared`는 벡터가 있어 비교한 트랙 수다(R-21)."""

    links: tuple[ReidLink, ...]
    compared: int


def mean_unit(vectors: Iterable[Sequence[float]]) -> tuple[float, ...] | None:
    """벡터 합을 단위 벡터로 만든다. 벡터가 없거나 합이 0이면 None이다."""
    total: list[float] | None = None
    for vector in vectors:
        total = (
            list(vector) if total is None else [a + b for a, b in zip(total, vector, strict=True)]
        )
    return unit_or_none(total)


def unit_or_none(total: Sequence[float] | None) -> tuple[float, ...] | None:
    """`total`을 길이 1로 맞춘다. 없거나 길이가 0이면 None이다."""
    if total is None:
        return None
    norm = math.sqrt(math.sumprod(total, total))
    if norm == 0:
        return None
    return tuple(v / norm for v in total)


def link_reappearances(groups: Sequence[Group], profile: ReidProfile) -> ReidResult:
    """같은 클래스, 시간이 겹치지 않고 간격이 최대 간격 이하, 유사도가 경계값 이상인 쌍을 잇는다.

    (유사도 내림차순, 앞 트랙, 뒤 트랙) 순서로 보며 앞 트랙에 다음이 없고 뒤 트랙에 이전이 없을
    때만 잇는다. 뒤 트랙은 앞 트랙이 끝난 뒤에 시작하므로 연결이 고리를 만들지 않는다.
    """
    usable = sorted((g for g in groups if g.vector is not None), key=lambda g: g.first.order_key())
    by_class: dict[str, list[Group]] = {}
    for group in usable:
        by_class.setdefault(group.class_name, []).append(group)
    candidates: list[tuple[float, Group, Group]] = []
    for same in by_class.values():
        starts = [g.start_ms for g in same]
        for earlier in same:
            lo = bisect_left(starts, earlier.end_ms)
            hi = bisect_right(starts, earlier.end_ms + profile.max_gap_ms)
            for later in same[lo:hi]:
                score = _cosine(earlier, later)
                if score >= profile.min_similarity:
                    candidates.append((score, earlier, later))
    candidates.sort(key=lambda c: (-c[0], c[1].first.order_key(), c[2].first.order_key()))
    has_next: set[TrackRef] = set()
    has_prev: set[TrackRef] = set()
    links: list[ReidLink] = []
    for score, earlier, later in candidates:
        if earlier.first.ref in has_next or later.first.ref in has_prev:
            continue
        has_next.add(earlier.first.ref)
        has_prev.add(later.first.ref)
        links.append(ReidLink(earlier, later, score, later.start_ms - earlier.end_ms))
    return ReidResult(tuple(links), len(usable))


def _cosine(a: Group, b: Group) -> float:
    if a.vector is None or b.vector is None:  # pragma: no cover - usable만 들어온다
        return -1.0
    # 단위 벡터 내적이 코사인 유사도다. 부동소수 오차로 계약 범위(-1~1)를 넘지 않게 자른다.
    return max(-1.0, min(1.0, math.sumprod(a.vector, b.vector)))
