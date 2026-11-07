"""재등장 연결 순수 함수(SPEC-022 R-9, R-10)."""

from collections.abc import Sequence
from uuid import UUID

import pytest
from stitcher_support import PROFILE, unit
from track_stitcher.model import LocalTrack, TrackRef
from track_stitcher.profile import ReidProfile
from track_stitcher.reid import Group, ReidResult, link_reappearances, mean_unit


def _group(
    name: str,
    start: int,
    end: int,
    vector: Sequence[float] | None,
    *,
    class_name: str = "person",
    chunk: int = 0,
) -> Group:
    member = LocalTrack(
        TrackRef(UUID(int=chunk), UUID(int=100 + chunk), name), class_name, start, end
    )
    return Group.of([member], tuple(vector) if vector is not None else None)


def _names(result: ReidResult) -> list[tuple[str, str]]:
    return [
        (x.earlier.first.ref.track_local_id, x.later.first.ref.track_local_id) for x in result.links
    ]


SAME = unit(1, 0, 0, 0)
CLOSE = unit(1, 0.2, 0, 0)  # SAME와 유사도 약 0.98
FAR = unit(0, 1, 0, 0)


def test_spec022_r9_similar_same_class_reappearance_is_linked() -> None:
    a = _group("a", 0, 10_000, SAME)
    b = _group("b", 40_000, 50_000, CLOSE, chunk=1)

    result = link_reappearances([b, a], PROFILE.reid)

    assert _names(result) == [("a", "b")]
    [link] = result.links
    assert link.score == pytest.approx(sum(x * y for x, y in zip(SAME, CLOSE, strict=True)))
    assert link.gap_ms == 30_000
    assert result.compared == 2


def test_spec022_r9_us2_ac2_low_similarity_stays_separate() -> None:
    result = link_reappearances(
        [_group("a", 0, 10_000, SAME), _group("b", 40_000, 50_000, FAR)], PROFILE.reid
    )

    assert result.links == ()


def test_spec022_r9_gap_limit_overlap_and_class() -> None:
    a = _group("a", 0, 10_000, SAME)
    far = _group("far", 70_001, 80_000, SAME)
    edge = _group("edge", 70_000, 80_000, SAME, chunk=2)
    overlap = _group("ov", 9_999, 20_000, SAME, chunk=3)
    forklift = _group("fk", 20_000, 30_000, SAME, class_name="forklift", chunk=4)

    assert link_reappearances([a, far], PROFILE.reid).links == ()
    assert _names(link_reappearances([a, edge], PROFILE.reid)) == [("a", "edge")]
    assert link_reappearances([a, overlap], PROFILE.reid).links == ()
    assert link_reappearances([a, forklift], PROFILE.reid).links == ()


def test_spec022_r9_zero_gap_is_allowed() -> None:
    result = link_reappearances(
        [_group("a", 0, 10_000, SAME), _group("b", 10_000, 20_000, SAME, chunk=1)], PROFILE.reid
    )

    assert [x.gap_ms for x in result.links] == [0]


def test_spec022_r9_each_track_links_at_most_one_before_and_after() -> None:
    a = _group("a", 0, 10_000, SAME)
    best = _group("b", 20_000, 30_000, SAME, chunk=1)
    second = _group("c", 21_000, 31_000, CLOSE, chunk=2)
    later = _group("d", 40_000, 50_000, CLOSE, chunk=3)

    result = link_reappearances([a, best, second, later], PROFILE.reid)

    # a→b(1.0), b→d·c→d 중 d는 앞 하나만. a→c는 a의 뒤가 이미 차서 안 된다.
    links = set(_names(result))
    assert ("a", "b") in links
    assert sum(1 for x in links if x[0] == "a") == 1
    assert sum(1 for x in links if x[1] == "d") == 1
    assert ("a", "c") not in links


def test_spec022_r9_equal_scores_are_deterministic() -> None:
    groups = [
        _group("a", 0, 10_000, SAME),
        _group("b", 0, 10_000, SAME, chunk=1),
        _group("c", 20_000, 30_000, SAME, chunk=2),
    ]

    first = _names(link_reappearances(groups, PROFILE.reid))
    second = _names(link_reappearances(list(reversed(groups)), PROFILE.reid))

    assert first == second == [("a", "c")]


def test_spec022_r10_tracks_without_vectors_are_not_candidates() -> None:
    result = link_reappearances(
        [_group("a", 0, 10_000, None), _group("b", 20_000, 30_000, SAME, chunk=1)], PROFILE.reid
    )

    assert result.links == ()
    assert result.compared == 1


def test_spec022_r9_threshold_is_inclusive() -> None:
    profile = ReidProfile(min_similarity=0.6, max_gap_ms=1000)
    other = (0.6, 0.8, 0.0, 0.0)

    result = link_reappearances(
        [_group("a", 0, 10, SAME), _group("b", 20, 30, other, chunk=1)], profile
    )

    assert len(result.links) == 1


def test_mean_unit_normalizes_sum_and_rejects_zero() -> None:
    assert mean_unit([[2.0, 0.0], [0.0, 2.0]]) == pytest.approx((2**-0.5, 2**-0.5))
    assert mean_unit([[1.0, 0.0], [-1.0, 0.0]]) is None
    assert mean_unit([]) is None
