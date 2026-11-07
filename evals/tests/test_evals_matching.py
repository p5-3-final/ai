"""매칭 규칙(SPEC-030 R-11·R-12)."""

import pytest
from evals.matching import (
    IntervalLabel,
    IntervalPrediction,
    MatchResult,
    match_intervals,
    temporal_iou,
)


def _label(id_: str, start_s: float, end_s: float, event_type: str = "fall") -> IntervalLabel:
    return IntervalLabel(id_, event_type, int(start_s * 1000), int(end_s * 1000))


def _pred(id_: str, start_s: float, end_s: float, event_type: str = "fall") -> IntervalPrediction:
    return IntervalPrediction(id_, event_type, int(start_s * 1000), int(end_s * 1000))


def _ids[L: IntervalLabel, P: IntervalPrediction](
    result: MatchResult[L, P],
) -> tuple[list[tuple[str, str]], list[str], list[str]]:
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
