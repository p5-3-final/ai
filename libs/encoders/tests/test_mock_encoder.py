import math

from vlm_encoders.base import ImageTextEncoder
from vlm_encoders.mock import MockEncoder


def _encoder() -> ImageTextEncoder:
    return MockEncoder("mock:mock:mock:mean-4", 8)


def test_spec011_r4_mock_vectors_have_unit_length_and_dimension() -> None:
    """[SPEC-011 R-4] 벡터는 프로필 차원이고 길이 1이다."""
    vectors = [*_encoder().encode_texts(["a", "b"]), *_encoder().encode_images([b"x"])]

    assert all(len(v) == 8 for v in vectors)
    assert all(math.isclose(math.hypot(*v), 1.0, rel_tol=1e-9) for v in vectors)


def test_spec011_r5_mock_is_deterministic_and_input_dependent() -> None:
    """[SPEC-011 R-5] 같은 입력이면 같은 벡터, 다른 입력이면 다른 벡터다."""
    assert _encoder().encode_texts(["forklift"]) == _encoder().encode_texts(["forklift"])
    assert _encoder().encode_texts(["forklift"]) != _encoder().encode_texts(["person"])
    assert _encoder().encode_images([b"x"]) == _encoder().encode_images([b"x"])
