"""매칭 규칙(SPEC-030 R-11·R-12)."""

from typing import Protocol

import pytest
from evals.matching import (
    Box,
    BoxLabel,
    BoxPrediction,
    IntervalLabel,
    IntervalPrediction,
    MatchResult,
    StateChangeLabel,
    StateChangePrediction,
    box_iou,
    match_boxes,
    match_intervals,
    match_state_changes,
    state_change_time_ms,
    temporal_iou,
)


class _HasId(Protocol):
    @property
    def id(self) -> str: ...


def _label(id_: str, start_s: float, end_s: float, event_type: str = "fall") -> IntervalLabel:
    return IntervalLabel(id_, event_type, int(start_s * 1000), int(end_s * 1000))


def _pred(id_: str, start_s: float, end_s: float, event_type: str = "fall") -> IntervalPrediction:
    return IntervalPrediction(id_, event_type, int(start_s * 1000), int(end_s * 1000))


def _ids(result: MatchResult[_HasId, _HasId]) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    return (
        [(p.label.id, p.prediction.id) for p in result.pairs],
        [label.id for label in result.unmatched_labels],
        [pred.id for pred in result.unmatched_predictions],
    )


@pytest.mark.parametrize(
    ("label", "prediction", "expected"),
    [
        ((10_000, 20_000), (12_000, 22_000), 8 / 12),
        ((0, 10_000), (10_000, 20_000), 0.0),
        ((0, 10_000), (2_000, 4_000), 0.2),
        ((0, 10_000), (0, 10_000), 1.0),
    ],
    ids=["spec-example", "touching", "contained", "same"],
)
def test_spec030_r11_temporal_iou_is_overlap_over_union(
    label: tuple[int, int], prediction: tuple[int, int], expected: float
) -> None:
    """[SPEC-030 R-11] temporal IoU = 겹친 길이 / 합친 길이."""
    assert temporal_iou(*label, *prediction) == pytest.approx(expected)


def test_spec030_r11_spec_example_rounds_to_0_667() -> None:
    """[SPEC-030 R-11] spec 예시 [10s, 20s]·[12s, 22s]는 0.667이다."""
    assert round(temporal_iou(10_000, 20_000, 12_000, 22_000), 3) == 0.667


def test_spec030_r11_zero_length_intervals_have_zero_iou() -> None:
    """[SPEC-030 R-11] 합친 길이가 0이면 나누지 않고 0이다."""
    assert temporal_iou(5_000, 5_000, 5_000, 5_000) == 0.0


def test_spec030_r11_reversed_interval_is_rejected() -> None:
    """[SPEC-030 R-11] 끝이 시작보다 앞선 구간은 받지 않는다."""
    with pytest.raises(ValueError, match="end"):
        temporal_iou(20_000, 10_000, 0, 5_000)


def test_spec030_r11_spec_example_pairs_with_iou_0_667() -> None:
    """[SPEC-030 R-11] 라벨 [10s, 20s] fall과 예측 [12s, 22s] fall은 0.667로 짝지어진다."""
    result = match_intervals([_label("L1", 10, 20)], [_pred("P1", 12, 22)])

    assert _ids(result) == ([("L1", "P1")], [], [])
    assert round(result.pairs[0].score, 3) == 0.667


def test_spec030_r11_only_same_event_type_is_compared() -> None:
    """[SPEC-030 R-11] 유형이 다르면 겹쳐도 비교하지 않는다(놓침 하나, 오탐 하나)."""
    result = match_intervals([_label("L1", 10, 20)], [_pred("P1", 10, 20, "smoke")])

    assert _ids(result) == ([], ["L1"], ["P1"])


def test_spec030_r11_threshold_is_inclusive() -> None:
    """[SPEC-030 R-11] IoU가 기준과 같으면 짝짓고, 기준보다 작으면 짝짓지 않는다."""
    exact = match_intervals([_label("L1", 0, 10)], [_pred("P1", 7, 10)])
    below = match_intervals([_label("L1", 0, 10)], [_pred("P1", 7.001, 10)])
    custom = match_intervals([_label("L1", 0, 10)], [_pred("P1", 7, 10)], min_iou=0.31)

    assert _ids(exact) == ([("L1", "P1")], [], [])
    assert _ids(below) == ([], ["L1"], ["P1"])
    assert _ids(custom) == ([], ["L1"], ["P1"])


def test_spec030_r11_two_predictions_for_one_label_keep_the_larger_iou() -> None:
    """[SPEC-030 R-11] 같은 라벨에 예측 둘이면 IoU가 큰 하나만 짝짓고 나머지는 오탐이다."""
    result = match_intervals([_label("L1", 10, 20)], [_pred("P1", 12, 22), _pred("P2", 10, 19)])

    assert _ids(result) == ([("L1", "P2")], [], ["P1"])


def test_spec030_r11_tie_goes_to_the_earlier_label_start() -> None:
    """[SPEC-030 R-11] IoU가 같으면 라벨 시작이 이른 쪽이 먼저 짝을 얻는다."""
    # 예측 [5s, 15s]는 두 라벨과 IoU가 모두 1/3이다.
    labels = [_label("L-late", 10, 20), _label("L-early", 0, 10)]
    result = match_intervals(labels, [_pred("P1", 5, 15)])

    assert _ids(result) == ([("L-early", "P1")], ["L-late"], [])


def test_spec030_r11_one_to_one_uses_greedy_order() -> None:
    """[SPEC-030 R-11] 큰 IoU 쌍이 먼저 짝을 가져가고 남은 쌍만 다음 순서로 짝짓는다."""
    labels = [_label("L1", 0, 10), _label("L2", 4, 14)]
    preds = [_pred("P1", 1, 10), _pred("P2", 5, 14)]
    result = match_intervals(labels, preds)

    assert _ids(result) == ([("L1", "P1"), ("L2", "P2")], [], [])


def test_spec030_r11_result_does_not_depend_on_input_order() -> None:
    """[SPEC-030 R-11] 입력 순서를 섞어도 결과가 같다."""
    labels = [_label(f"L{i}", i * 3, i * 3 + 5) for i in range(8)]
    labels += [_label("S1", 0, 30, "smoke")]
    preds = [_pred(f"P{i}", i * 3 + 1, i * 3 + 6) for i in range(8)]
    preds += [_pred("P-extra", 2, 7), _pred("S-pred", 1, 29, "smoke")]
    expected = match_intervals(labels, preds)

    for shift in range(1, len(labels)):
        shuffled_labels = labels[shift:] + labels[:shift]
        shuffled_preds = (preds[shift:] + preds[:shift])[::-1]
        assert match_intervals(shuffled_labels, shuffled_preds) == expected


def test_spec030_r11_duplicate_ids_are_rejected() -> None:
    """[SPEC-030 R-11] ID가 겹치면 순서 규칙이 정해지지 않아 받지 않는다."""
    with pytest.raises(ValueError, match="duplicate"):
        match_intervals([_label("L1", 0, 1), _label("L1", 2, 3)], [])


_FULL = Box(0.1, 0.1, 0.4, 0.4)


def _box_label(id_: str, pts_ms: int, box: Box = _FULL, class_name: str = "person") -> BoxLabel:
    return BoxLabel(id_, pts_ms, class_name, box)


def _box_pred(id_: str, pts_ms: int, box: Box = _FULL, class_name: str = "person") -> BoxPrediction:
    return BoxPrediction(id_, pts_ms, class_name, box)


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (Box(0, 0, 0.5, 0.5), Box(0, 0, 0.5, 0.5), 1.0),
        (Box(0, 0, 0.5, 0.5), Box(0.25, 0, 0.5, 0.5), 1 / 3),
        (Box(0, 0, 0.2, 0.2), Box(0.5, 0.5, 0.2, 0.2), 0.0),
        (Box(0, 0, 0.2, 0.2), Box(0.2, 0, 0.2, 0.2), 0.0),
        (Box(0.3, 0.3, 0, 0), Box(0.3, 0.3, 0, 0), 0.0),
    ],
    ids=["same", "half-shift", "apart", "touching", "zero-area"],
)
def test_spec030_r12_box_iou_is_intersection_over_union(a: Box, b: Box, expected: float) -> None:
    """[SPEC-030 R-12] 박스 IoU = 겹친 넓이 / 합친 넓이, 합친 넓이가 0이면 0."""
    assert box_iou(a, b) == pytest.approx(expected)


def test_spec030_r12_box_time_difference_up_to_half_frame_interval() -> None:
    """[SPEC-030 R-12] 표시 시각 차가 프레임 간격의 절반이면 짝짓고, 넘으면 짝짓지 않는다."""
    at_half = match_boxes([_box_label("L1", 1000)], [_box_pred("P1", 1020)], frame_interval_ms=40)
    over = match_boxes([_box_label("L1", 1000)], [_box_pred("P1", 1021)], frame_interval_ms=40)
    before = match_boxes([_box_label("L1", 1000)], [_box_pred("P1", 980)], frame_interval_ms=40)

    assert _ids(at_half) == ([("L1", "P1")], [], [])
    assert _ids(over) == ([], ["L1"], ["P1"])
    assert _ids(before) == ([("L1", "P1")], [], [])


def test_spec030_r12_box_requires_same_class_and_iou_threshold() -> None:
    """[SPEC-030 R-12] 클래스가 다르거나 박스 IoU가 기준(기본 0.5) 미만이면 짝짓지 않는다."""
    other_class = match_boxes(
        [_box_label("L1", 0)], [_box_pred("P1", 0, class_name="forklift")], frame_interval_ms=40
    )
    # 너비 절반 이동: IoU 1/3
    shifted = Box(_FULL.x + 0.2, _FULL.y, _FULL.w, _FULL.h)
    low_iou = match_boxes(
        [_box_label("L1", 0)], [_box_pred("P1", 0, shifted)], frame_interval_ms=40
    )
    custom = match_boxes(
        [_box_label("L1", 0)], [_box_pred("P1", 0, shifted)], frame_interval_ms=40, min_iou=0.3
    )

    assert _ids(other_class) == ([], ["L1"], ["P1"])
    assert _ids(low_iou) == ([], ["L1"], ["P1"])
    assert _ids(custom) == ([("L1", "P1")], [], [])


def test_spec030_r12_box_uses_the_same_order_rule() -> None:
    """[SPEC-030 R-12] IoU 큰 순, 같으면 라벨 시각이 이른 순으로 일대일 짝짓는다."""
    near = Box(_FULL.x + 0.02, _FULL.y, _FULL.w, _FULL.h)
    two_preds = match_boxes(
        [_box_label("L1", 0)], [_box_pred("P1", 0, near), _box_pred("P2", 0)], frame_interval_ms=40
    )
    tie = match_boxes(
        [_box_label("L-late", 20), _box_label("L-early", 0)],
        [_box_pred("P1", 10)],
        frame_interval_ms=40,
    )

    assert _ids(two_preds) == ([("L1", "P2")], [], ["P1"])
    assert _ids(tie) == ([("L-early", "P1")], ["L-late"], [])


def test_spec030_r12_box_frame_interval_must_be_positive() -> None:
    """[SPEC-030 R-12] 프레임 간격은 0보다 커야 한다."""
    with pytest.raises(ValueError, match="frame_interval_ms"):
        match_boxes([], [], frame_interval_ms=0)


def _change(id_: str, at_s: float, change: str, class_name: str = "box") -> StateChangeLabel:
    return StateChangeLabel(id_, int(at_s * 1000), class_name, change)


def _inventory(id_: str, at_s: float, kind: str, class_name: str = "box") -> StateChangePrediction:
    return StateChangePrediction(id_, int(at_s * 1000), class_name, kind)


@pytest.mark.parametrize(
    ("change", "kind"),
    [("removed", "removed"), ("moved", "moved"), ("added", "appeared")],
)
def test_spec030_r12_state_change_default_kind_map(change: str, kind: str) -> None:
    """[SPEC-030 R-12] 기본 대응표: removed↔removed, moved↔moved, added↔appeared."""
    result = match_state_changes([_change("L1", 10, change)], [_inventory("P1", 10, kind)])

    assert _ids(result) == ([("L1", "P1")], [], [])


def test_spec030_r12_restored_and_not_restored_never_pair_by_default() -> None:
    """[SPEC-030 R-12] restored 라벨은 항상 놓침, not_restored 예측은 항상 오탐이다."""
    result = match_state_changes(
        [_change("L1", 10, "restored")], [_inventory("P1", 10, "not_restored")]
    )

    assert _ids(result) == ([], ["L1"], ["P1"])


def test_spec030_r12_state_change_kind_map_is_an_argument() -> None:
    """[SPEC-030 R-12] 대응표를 인자로 바꿀 수 있다."""
    result = match_state_changes(
        [_change("L1", 10, "restored")],
        [_inventory("P1", 10, "not_restored")],
        kinds={"restored": "not_restored"},
    )

    assert _ids(result) == ([("L1", "P1")], [], [])


def test_spec030_r12_state_change_needs_same_class_and_kind() -> None:
    """[SPEC-030 R-12] 클래스나 변화 종류가 다르면 짝짓지 않는다."""
    other_class = match_state_changes(
        [_change("L1", 10, "removed")], [_inventory("P1", 10, "removed", "pallet")]
    )
    other_kind = match_state_changes(
        [_change("L1", 10, "removed")], [_inventory("P1", 10, "moved")]
    )

    assert _ids(other_class) == ([], ["L1"], ["P1"])
    assert _ids(other_kind) == ([], ["L1"], ["P1"])


def test_spec030_r12_state_change_time_difference_threshold_is_inclusive() -> None:
    """[SPEC-030 R-12] 시각 차가 기준(기본 5초)과 같으면 짝짓고, 넘으면 짝짓지 않는다."""
    at_limit = match_state_changes(
        [_change("L1", 10, "removed")], [_inventory("P1", 15, "removed")]
    )
    over = match_state_changes(
        [_change("L1", 10, "removed")], [_inventory("P1", 15.001, "removed")]
    )
    custom = match_state_changes(
        [_change("L1", 10, "removed")], [_inventory("P1", 15, "removed")], max_delta_ms=4999
    )

    assert _ids(at_limit) == ([("L1", "P1")], [], [])
    assert at_limit.pairs[0].score == 5000
    assert _ids(over) == ([], ["L1"], ["P1"])
    assert _ids(custom) == ([], ["L1"], ["P1"])


def test_spec030_r12_state_change_closest_first_then_earlier_label() -> None:
    """[SPEC-030 R-12] 시각 차가 작은 순, 같으면 라벨 시각이 이른 순으로 일대일 짝짓는다."""
    closest = match_state_changes(
        [_change("L1", 10, "removed")],
        [_inventory("P-far", 13, "removed"), _inventory("P-near", 9, "removed")],
    )
    tie = match_state_changes(
        [_change("L-late", 12, "removed"), _change("L-early", 8, "removed")],
        [_inventory("P1", 10, "removed")],
    )

    assert _ids(closest) == ([("L1", "P-near")], [], ["P-far"])
    assert _ids(tie) == ([("L-early", "P1")], ["L-late"], [])


@pytest.mark.parametrize(
    ("before_ms", "after_ms", "expected"),
    [(1000, 4000, 4000), (1000, None, 1000), (None, 4000, 4000), (None, None, None)],
)
def test_spec030_r12_inventory_change_time_prefers_after_ms(
    before_ms: int | None, after_ms: int | None, expected: int | None
) -> None:
    """[SPEC-030 R-12] 재고 변화 시각은 afterMs, 없으면 beforeMs다. 둘 다 없으면 비교할 수 없다."""
    assert state_change_time_ms(before_ms=before_ms, after_ms=after_ms) == expected


def test_spec030_r12_max_delta_must_not_be_negative() -> None:
    """[SPEC-030 R-12] 시각 차 기준은 0 이상이다."""
    with pytest.raises(ValueError, match="max_delta_ms"):
        match_state_changes([], [], max_delta_ms=-1)
