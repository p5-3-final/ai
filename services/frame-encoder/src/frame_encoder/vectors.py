"""구간·프로토타입 벡터 계산. 입력과 출력 벡터는 모두 같은 인코더 공간에 있다."""

import math

from vlm_encoders.base import Vector


def normalize(vector: Vector) -> Vector:
    """길이(L2 노름) 1로 맞춘다. 영벡터는 방향이 없어 거절한다."""
    norm = math.hypot(*vector)
    if norm == 0:
        raise ValueError("cannot normalize a zero vector")
    return [x / norm for x in vector]


def normalized_mean(vectors: list[Vector]) -> Vector:
    """각 벡터를 정규화해 평균한 뒤 다시 정규화한다.

    SPEC-019 R-1(구간 벡터)·R-10(프로토타입 벡터). 먼저 정규화해 길이가 큰 벡터가 평균을
    끌고 가지 않게 한다.
    """
    units = [normalize(v) for v in vectors]
    return normalize([sum(column) / len(units) for column in zip(*units, strict=True)])


def segment_vector(frames: list[Vector]) -> Vector:
    """구간 프레임 4장의 벡터로 구간 벡터를 만든다(SPEC-019 R-1)."""
    return normalized_mean(frames)


def cosine(a: Vector, b: Vector) -> float:
    """길이 1인 두 벡터의 코사인 유사도(내적)."""
    return math.sumprod(a, b)
