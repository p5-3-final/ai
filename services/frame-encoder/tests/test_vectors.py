import math
import random

import pytest
from frame_encoder.vectors import cosine, normalize, segment_vector


def _norm(v: list[float]) -> float:
    return math.hypot(*v)


def test_spec_019_r1_segment_vector_is_renormalized_mean_of_four_frames() -> None:
    frames = [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [0.0, 1.0]]

    vector = segment_vector(frames)

    assert vector == pytest.approx([math.sqrt(0.5), math.sqrt(0.5)])
    assert _norm(vector) == pytest.approx(1.0)


def test_spec_019_r1_frames_are_normalized_before_averaging() -> None:
    # 길이가 다른 벡터가 평균을 끌고 가지 않아야 한다.
    frames = [[10.0, 0.0], [0.0, 1.0], [0.0, 1.0], [0.0, 1.0]]

    vector = segment_vector(frames)

    expected = [0.25 / math.hypot(0.25, 0.75), 0.75 / math.hypot(0.25, 0.75)]
    assert vector == pytest.approx(expected)


def test_cosine_of_unit_vectors() -> None:
    assert cosine([1.0, 0.0], [math.sqrt(0.5), math.sqrt(0.5)]) == pytest.approx(math.sqrt(0.5))


def test_cosine_stays_within_contract_score_range() -> None:
    # 길이 1인 같은 벡터의 내적도 반올림 오차로 1을 넘을 수 있다. 후보 점수는 계약상 -1~1이다.
    rng = random.Random(1)
    vectors = [normalize([rng.uniform(-1, 1) for _ in range(512)]) for _ in range(50)]

    for v in vectors:
        assert -1.0 <= cosine(v, v) <= 1.0
        assert -1.0 <= cosine(v, [-x for x in v]) <= 1.0
