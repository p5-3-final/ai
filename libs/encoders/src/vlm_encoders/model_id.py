"""인코더 모델 ID(docs overview "모델과 플랫폼 교체": `<라이브러리>:<아키텍처>:<가중치>:<집계>`)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelId:
    """벡터의 `embedding_model`로 기록하는 ID.

    집계(`mean-4`: 구간 프레임 4장 평균)는 frame-encoder가 쓰는 부분이다. 질의 벡터에는 쓰지 않지만
    같은 인덱스를 찾아야 하므로 ID는 그대로 둔다(SPEC-011 R-6).
    """

    library: str
    architecture: str
    weights: str
    aggregation: str

    @classmethod
    def parse(cls, value: str) -> "ModelId":
        """모델 ID 문자열을 나눈다.

        Raises:
            ValueError: 네 부분이 모두 있지 않을 때.
        """
        parts = value.split(":")
        if len(parts) != 4 or not all(parts):
            raise ValueError(f"invalid model id {value!r}: expected library:arch:weights:agg")
        return cls(*parts)

    def __str__(self) -> str:
        return f"{self.library}:{self.architecture}:{self.weights}:{self.aggregation}"
