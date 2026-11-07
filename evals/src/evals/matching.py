"""평가 잡이 예측과 라벨을 맞추는 규칙(SPEC-030 R-11·R-12).

세 종류 모두 같은 일대일 탐욕 규칙을 쓴다. 기준을 넘은 후보 쌍을 점수 순서로 정렬해 앞에서부터
라벨과 예측이 둘 다 아직 짝이 없으면 짝짓는다. 짝이 없는 라벨은 놓침, 짝이 없는 예측은 오탐이다.
결과 순서는 입력 순서와 상관없이 정해진다.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class IntervalLabel:
    """시간 구간 라벨(`golden-manifest` `intervalLabel`)."""

    id: str
    event_type: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class IntervalPrediction:
    """예측 이벤트 구간."""

    id: str
    event_type: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Box:
    """프레임 크기로 나눈 0~1 좌표의 박스. 왼쪽 위 기준이다."""

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class BoxLabel:
    """bbox 라벨(`golden-manifest` `bboxLabel`)."""

    id: str
    pts_ms: int
    class_name: str
    box: Box


@dataclass(frozen=True, slots=True)
class BoxPrediction:
    """검출 하나."""

    id: str
    pts_ms: int
    class_name: str
    box: Box


@dataclass(frozen=True, slots=True)
class StateChangeLabel:
    """상태 변화 라벨(`golden-manifest` `stateChangeLabel`). `change`는 라벨의 변화 종류다."""

    id: str
    at_ms: int
    class_name: str
    change: str


@dataclass(frozen=True, slots=True)
class StateChangePrediction:
    """재고 변화(`source-result` `inventoryChanges`). `kind`는 결과의 변화 종류다."""

    id: str
    at_ms: int
    class_name: str
    kind: str


@dataclass(frozen=True, slots=True)
class MatchedPair[L, P]:
    """짝지은 라벨과 예측. `score`는 IoU이거나(구간·bbox) 시각 차 ms의 절댓값(상태 변화)이다."""

    label: L
    prediction: P
    score: float


@dataclass(frozen=True, slots=True)
class MatchResult[L, P]:
    """매칭 결과. 짝이 없는 라벨은 놓침, 짝이 없는 예측은 오탐이다."""

    pairs: tuple[MatchedPair[L, P], ...]
    unmatched_labels: tuple[L, ...]
    unmatched_predictions: tuple[P, ...]


DEFAULT_STATE_CHANGE_KINDS: Mapping[str, str] = {
    "removed": "removed",
    "moved": "moved",
    "added": "appeared",
}
"""라벨 `change` → 결과 `kind` 기본 대응표."""


def temporal_iou(a_start_ms: int, a_end_ms: int, b_start_ms: int, b_end_ms: int) -> float:
    """두 구간의 temporal IoU(겹친 길이 / 합친 길이).

    Raises:
        ValueError: 끝이 시작보다 앞선 구간.
    """
    _check_interval(a_start_ms, a_end_ms)
    _check_interval(b_start_ms, b_end_ms)
    overlap = max(0, min(a_end_ms, b_end_ms) - max(a_start_ms, b_start_ms))
    union = (a_end_ms - a_start_ms) + (b_end_ms - b_start_ms) - overlap
    # 길이 0인 구간 둘이 같은 시각이면 합친 길이가 0이다. 기준을 넘지 않게 0으로 본다.
    return overlap / union if union > 0 else 0.0


def _check_interval(start_ms: int, end_ms: int) -> None:
    if end_ms < start_ms:
        raise ValueError(f"interval end {end_ms} is before start {start_ms}")


def box_iou(a: Box, b: Box) -> float:
    """두 박스의 IoU(겹친 넓이 / 합친 넓이)."""
    raise NotImplementedError


def match_intervals(
    labels: Sequence[IntervalLabel],
    predictions: Sequence[IntervalPrediction],
    *,
    min_iou: float = 0.3,
) -> MatchResult[IntervalLabel, IntervalPrediction]:
    """시간 구간 라벨과 예측 이벤트를 짝짓는다(SPEC-030 R-11).

    같은 이벤트 유형끼리만 비교하고, temporal IoU가 `min_iou` 이상인 쌍을 IoU 큰 순서(같으면 라벨
    시작이 이른 순, 그다음 라벨 ID·예측 ID 순)로 일대일로 짝짓는다.

    Raises:
        ValueError: 기준이 (0, 1] 밖이거나, ID가 겹치거나, 끝이 시작보다 앞선 구간.
    """
    _check_ratio("min_iou", min_iou)

    def score(label: IntervalLabel, pred: IntervalPrediction) -> float | None:
        if label.event_type != pred.event_type:
            return None
        iou = temporal_iou(label.start_ms, label.end_ms, pred.start_ms, pred.end_ms)
        return iou if iou >= min_iou else None

    return _greedy_match(
        labels,
        predictions,
        score=score,
        larger_is_better=True,
        label_time=lambda label: label.start_ms,
        prediction_time=lambda pred: pred.start_ms,
    )


def match_boxes(
    labels: Sequence[BoxLabel],
    predictions: Sequence[BoxPrediction],
    *,
    frame_interval_ms: float,
    min_iou: float = 0.5,
) -> MatchResult[BoxLabel, BoxPrediction]:
    """bbox 라벨과 검출을 짝짓는다(SPEC-030 R-12)."""
    raise NotImplementedError


def match_state_changes(
    labels: Sequence[StateChangeLabel],
    predictions: Sequence[StateChangePrediction],
    *,
    max_delta_ms: int = 5000,
    kinds: Mapping[str, str] = DEFAULT_STATE_CHANGE_KINDS,
) -> MatchResult[StateChangeLabel, StateChangePrediction]:
    """상태 변화 라벨과 재고 변화를 짝짓는다(SPEC-030 R-12)."""
    raise NotImplementedError


def _check_ratio(name: str, value: float) -> None:
    if not 0 < value <= 1:
        raise ValueError(f"{name} must be in (0, 1], got {value}")


class _Identified(Protocol):
    @property
    def id(self) -> str: ...


def _check_unique_ids(kind: str, items: Sequence[_Identified]) -> None:
    ids = [item.id for item in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate {kind} id")


def _greedy_match[L: _Identified, P: _Identified](
    labels: Sequence[L],
    predictions: Sequence[P],
    *,
    score: Callable[[L, P], float | None],
    larger_is_better: bool,
    label_time: Callable[[L], int],
    prediction_time: Callable[[P], int],
) -> MatchResult[L, P]:
    """기준을 통과한 쌍(`score`가 None이 아님)을 점수 순서로 일대일 짝짓는다.

    정렬 키는 (점수, 라벨 시각, 라벨 ID, 예측 ID)다. ID까지 넣어야 입력 순서와 상관없이 같은 결과가
    된다. 그래서 ID가 겹치면 받지 않는다.
    """
    _check_unique_ids("label", labels)
    _check_unique_ids("prediction", predictions)
    candidates: list[tuple[float, int, str, str, L, P, float]] = []
    for label in labels:
        for pred in predictions:
            value = score(label, pred)
            if value is None:
                continue
            key = -value if larger_is_better else value
            candidates.append((key, label_time(label), label.id, pred.id, label, pred, value))
    candidates.sort(key=lambda c: c[:4])

    pairs: list[MatchedPair[L, P]] = []
    used_labels: set[str] = set()
    used_predictions: set[str] = set()
    for _, _, label_id, pred_id, label, pred, value in candidates:
        if label_id in used_labels or pred_id in used_predictions:
            continue
        used_labels.add(label_id)
        used_predictions.add(pred_id)
        pairs.append(MatchedPair(label, pred, value))

    return MatchResult(
        pairs=tuple(pairs),
        unmatched_labels=tuple(
            sorted(
                (label for label in labels if label.id not in used_labels),
                key=lambda label: (label_time(label), label.id),
            )
        ),
        unmatched_predictions=tuple(
            sorted(
                (pred for pred in predictions if pred.id not in used_predictions),
                key=lambda pred: (prediction_time(pred), pred.id),
            )
        ),
    )
