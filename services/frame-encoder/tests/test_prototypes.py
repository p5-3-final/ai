import math

import pytest
from fe_support import encoder
from frame_encoder.profile import PrototypesProfile, PrototypeType
from frame_encoder.prototypes import Prototypes
from frame_encoder.vectors import cosine, normalized_mean
from vlm_encoders.base import Vector

FALL_TEXTS = ["a worker lying on the floor", "a person fallen down"]
FIRE_TEXTS = ["smoke rising from a machine"]


def _profile(fall: float = 0.2, fire: float = 0.2) -> PrototypesProfile:
    return PrototypesProfile(
        version="v1",
        types={
            "fall": PrototypeType(texts=FALL_TEXTS, threshold=fall),
            "fire_or_smoke": PrototypeType(texts=FIRE_TEXTS, threshold=fire),
        },
    )


def test_spec_019_r10_prototype_vector_is_normalized_mean_of_texts() -> None:
    mock = encoder()

    prototypes = Prototypes.build(_profile(), mock)

    fall = prototypes.vector("fall")
    assert fall == pytest.approx(normalized_mean(mock.encode_texts(FALL_TEXTS)))
    assert math.hypot(*fall) == pytest.approx(1.0)


def test_spec_019_r11_segment_matching_prototype_is_candidate_with_score() -> None:
    prototypes = Prototypes.build(_profile(), encoder())
    segment = prototypes.vector("fall")

    matches = prototypes.match(segment)

    fall = next(m for m in matches if m.event_type == "fall")
    assert fall.score == pytest.approx(1.0)
    assert fall.threshold == 0.2


def test_spec_019_r11_score_below_threshold_is_not_candidate() -> None:
    prototypes = Prototypes.build(_profile(fall=0.99, fire=0.99), encoder())
    # 두 프로토타입 어느 쪽과도 같지 않은 방향
    opposite: Vector = [-x for x in prototypes.vector("fall")]

    assert prototypes.match(opposite) == []


def test_spec_019_r11_score_equal_to_threshold_is_candidate() -> None:
    probe = Prototypes.build(_profile(), encoder())
    segment = probe.vector("fall")
    score = cosine(segment, probe.vector("fire_or_smoke"))

    prototypes = Prototypes.build(_profile(fall=0.99, fire=score), encoder())

    assert [m.event_type for m in prototypes.match(segment)] == ["fall", "fire_or_smoke"]
