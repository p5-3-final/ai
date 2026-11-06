"""텍스트 프로토타입으로 구간의 이벤트 후보를 고른다(SPEC-019 R-10, R-11).

학습 없이 유형마다 몇 문장을 인코더 텍스트 쪽으로 인코딩해 기준 벡터를 만들고, 구간 벡터와의
코사인 유사도가 임계값 이상이면 후보로 넘긴다. 판정은 후속 단계(event-analyzer)가 한다.
"""

from dataclasses import dataclass
from typing import Self

from vlm_contracts.events import DetectableEventType
from vlm_encoders.base import ImageTextEncoder, Vector

from frame_encoder.profile import PrototypesProfile
from frame_encoder.vectors import cosine, normalized_mean


@dataclass(frozen=True, slots=True)
class Match:
    """구간 하나가 넘은 유형 하나."""

    event_type: DetectableEventType
    score: float
    threshold: float


@dataclass(frozen=True, slots=True)
class _Prototype:
    vector: Vector
    threshold: float


class Prototypes:
    """유형별 프로토타입 벡터와 임계값. 기동할 때 한 번 만든다."""

    def __init__(self, version: str, prototypes: dict[DetectableEventType, _Prototype]) -> None:
        self.version = version
        self._prototypes = prototypes

    @classmethod
    def build(cls, profile: PrototypesProfile, encoder: ImageTextEncoder) -> Self:
        """유형마다 문장 벡터의 정규화 평균을 프로토타입으로 삼는다(R-10)."""
        return cls(
            profile.version,
            {
                event_type: _Prototype(normalized_mean(encoder.encode_texts(p.texts)), p.threshold)
                for event_type, p in profile.types.items()
            },
        )

    def vector(self, event_type: DetectableEventType) -> Vector:
        """유형의 프로토타입 벡터."""
        return self._prototypes[event_type].vector

    def match(self, segment: Vector) -> list[Match]:
        """점수가 임계값 이상인 유형을 프로필 순서대로 돌려준다(R-11)."""
        matches = []
        for event_type, prototype in self._prototypes.items():
            score = cosine(segment, prototype.vector)
            if score >= prototype.threshold:
                matches.append(Match(event_type, score, prototype.threshold))
        return matches
