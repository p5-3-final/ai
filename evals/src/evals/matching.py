"""평가 잡이 예측과 라벨을 맞추는 규칙(SPEC-030 R-11·R-12).

세 종류 모두 같은 일대일 탐욕 규칙을 쓴다. 기준을 넘은 후보 쌍을 점수 순서로 정렬해 앞에서부터
라벨과 예측이 둘 다 아직 짝이 없으면 짝짓는다. 짝이 없는 라벨은 놓침, 짝이 없는 예측은 오탐이다.
결과 순서는 입력 순서와 상관없이 정해진다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


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
    """두 구간의 temporal IoU(겹친 길이 / 합친 길이)."""
    raise NotImplementedError


def box_iou(a: Box, b: Box) -> float:
    """두 박스의 IoU(겹친 넓이 / 합친 넓이)."""
    raise NotImplementedError


def match_intervals(
    labels: Sequence[IntervalLabel],
    predictions: Sequence[IntervalPrediction],
    *,
    min_iou: float = 0.3,
) -> MatchResult[IntervalLabel, IntervalPrediction]:
    """시간 구간 라벨과 예측 이벤트를 짝짓는다(SPEC-030 R-11)."""
    raise NotImplementedError


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
