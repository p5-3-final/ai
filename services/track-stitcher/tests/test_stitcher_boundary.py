"""경계 연결 순수 함수(SPEC-022 R-3, R-4, R-6)."""

from dataclasses import dataclass
from uuid import UUID

import pytest
from stitcher_support import (
    LEFT,
    PROFILE,
    RIGHT,
    boxes,
    chunk_tracks,
    track,
)
from track_stitcher.boundary import adjacent_pairs, common_observations, iou, link_pair
from track_stitcher.profile import BoundaryProfile
from vlm_contracts.events import TimeRange
from vlm_contracts.tracks import BoundaryBox

# 청크 0과 1의 경계(120초) 양쪽 창: [115초, 125초)
HEAD_OF_1 = (115_000, 125_000)


@dataclass(frozen=True)
class _Chunk:
    chunk_id: UUID
    owned_range: TimeRange


def _chunk(n: int, start: int, end: int) -> _Chunk:
    return _Chunk(UUID(int=n), TimeRange(start_ms=start, end_ms=end))


def test_spec022_r3_chunks_touching_owned_ranges_are_adjacent() -> None:
    a, b, c = _chunk(1, 0, 100), _chunk(2, 100, 200), _chunk(3, 200, 300)

    assert adjacent_pairs([c, a, b]) == [(a, b), (b, c)]


def test_spec022_r3_gap_or_overlap_is_not_adjacent() -> None:
    a, gap, overlap = _chunk(1, 0, 100), _chunk(2, 101, 200), _chunk(3, 150, 300)

    assert adjacent_pairs([a, gap, overlap]) == []


def _box(pts: int, box: tuple[float, float, float, float] = LEFT) -> BoundaryBox:
    return BoundaryBox(pts_ms=pts, box=list(box))


def test_spec022_r6_pairs_boxes_within_tolerance_inside_window() -> None:
    earlier = [_box(114_900), _box(116_000), _box(117_000), _box(125_000)]
    later = [_box(116_050), _box(117_100), _box(117_101), _box(124_999)]

    pairs = common_observations(earlier, later, HEAD_OF_1, tolerance_ms=100)

    # 114.9초·125초는 창 밖, 117초는 정확히 100ms 차이까지 포함한다.
    assert [(a.pts_ms, b.pts_ms) for a, b in pairs] == [(116_000, 116_050), (117_000, 117_100)]


def test_spec022_r6_each_box_is_used_once_and_nearest_wins() -> None:
    earlier = [_box(116_000), _box(116_010)]
    later = [_box(116_005)]

    pairs = common_observations(earlier, later, HEAD_OF_1, tolerance_ms=100)

    assert [(a.pts_ms, b.pts_ms) for a, b in pairs] == [(116_000, 116_005)]


def test_spec022_r6_equal_distance_prefers_earlier_box() -> None:
    pairs = common_observations([_box(116_000)], [_box(116_010), _box(115_990)], HEAD_OF_1, 10)

    assert [b.pts_ms for _, b in pairs] == [115_990]


def test_iou_of_identical_and_disjoint_boxes() -> None:
    assert iou(LEFT, LEFT) == pytest.approx(1.0)
    assert iou(LEFT, RIGHT) == 0.0
    assert iou((0.0, 0.0, 0.2, 0.2), (0.1, 0.0, 0.3, 0.2)) == pytest.approx(1 / 3)


def test_spec022_r4_same_class_overlapping_tracks_are_linked() -> None:
    a = chunk_tracks(0, [track("a1", 100_000, 124_500, tail=boxes(115_000, 125_000))])
    b = chunk_tracks(1, [track("b1", 115_000, 150_000, head=boxes(115_000, 125_000))])

    result = link_pair(a, b, PROFILE.boundary)

    assert [
        (link.earlier.ref.track_local_id, link.later.ref.track_local_id) for link in result.links
    ] == [("a1", "b1")]
    assert result.links[0].score == pytest.approx(1.0)
    assert result.compared == 1


def test_spec022_r4_us1_ac2_different_class_is_not_linked() -> None:
    a = chunk_tracks(0, [track("a1", 100_000, 124_500, tail=boxes(115_000, 125_000))])
    b = chunk_tracks(
        1, [track("b1", 115_000, 150_000, class_name="forklift", head=boxes(115_000, 125_000))]
    )

    result = link_pair(a, b, PROFILE.boundary)

    assert result.links == ()
    assert result.compared == 0


def test_spec022_r4_only_tail_and_head_tracks_are_candidates() -> None:
    a = chunk_tracks(0, [track("a1", 100_000, 124_500, head=boxes(115_000, 125_000))])
    b = chunk_tracks(1, [track("b1", 115_000, 150_000, tail=boxes(115_000, 125_000))])

    assert link_pair(a, b, PROFILE.boundary).links == ()


def test_spec022_r4_mean_iou_and_minimum_observations_are_thresholds() -> None:
    low = BoundaryProfile(min_mean_iou=0.34, min_common_observations=2, time_tolerance_ms=0)
    a = chunk_tracks(
        0, [track("a1", 100_000, 124_500, tail=boxes(116_000, 117_000, (0, 0, 0.2, 0.2)))]
    )
    b = chunk_tracks(
        1, [track("b1", 115_000, 150_000, head=boxes(116_000, 117_000, (0.1, 0, 0.3, 0.2)))]
    )

    # IoU 1/3 < 0.34
    assert link_pair(a, b, low).links == ()
    at = BoundaryProfile(min_mean_iou=1 / 3, min_common_observations=2, time_tolerance_ms=0)
    assert len(link_pair(a, b, at).links) == 1
    # 공통 관측 2개(116.0초, 116.5초)뿐이라 3개를 요구하면 잇지 않는다.
    more = BoundaryProfile(min_mean_iou=0.3, min_common_observations=3, time_tolerance_ms=0)
    assert link_pair(a, b, more).links == ()


def test_spec022_r4_one_to_one_by_highest_iou() -> None:
    near = (0.1, 0.2, 0.3, 0.8)
    shifted = (0.12, 0.2, 0.32, 0.8)
    a = chunk_tracks(
        0,
        [
            track("a1", 100_000, 124_500, tail=boxes(115_000, 125_000, near)),
            track("a2", 100_000, 124_500, tail=boxes(115_000, 125_000, shifted)),
        ],
    )
    b = chunk_tracks(1, [track("b1", 115_000, 150_000, head=boxes(115_000, 125_000, near))])

    result = link_pair(a, b, PROFILE.boundary)

    assert [(x.earlier.ref.track_local_id, x.later.ref.track_local_id) for x in result.links] == [
        ("a1", "b1")
    ]
    assert result.compared == 2


def test_spec022_r4_equal_scores_link_deterministically() -> None:
    a = chunk_tracks(
        0,
        [
            track("a2", 100_000, 124_500, tail=boxes(115_000, 125_000)),
            track("a1", 100_000, 124_500, tail=boxes(115_000, 125_000)),
        ],
    )
    b = chunk_tracks(
        1,
        [
            track("b2", 115_000, 150_000, head=boxes(115_000, 125_000)),
            track("b1", 115_000, 150_000, head=boxes(115_000, 125_000)),
        ],
    )

    result = link_pair(a, b, PROFILE.boundary)

    assert [(x.earlier.ref.track_local_id, x.later.ref.track_local_id) for x in result.links] == [
        ("a1", "b1"),
        ("a2", "b2"),
    ]
