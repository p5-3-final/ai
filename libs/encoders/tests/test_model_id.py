import pytest
from vlm_encoders.model_id import ModelId


def test_spec011_r6_parses_profile_model_id() -> None:
    """[SPEC-011 R-6] frame-encoder와 같은 모델 ID 문자열을 네 부분으로 나눈다."""
    model = ModelId.parse("open_clip:MobileCLIP2-S0:dfndr2b:mean-4")

    assert model == ModelId("open_clip", "MobileCLIP2-S0", "dfndr2b", "mean-4")
    assert str(model) == "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"


@pytest.mark.parametrize(
    "value", ["", "open_clip:MobileCLIP2-S0:dfndr2b", "open_clip::dfndr2b:mean-4", "a:b:c:d:e"]
)
def test_spec011_r6_rejects_malformed_model_id(value: str) -> None:
    """[SPEC-011 R-6] 네 부분이 모두 있어야 한다."""
    with pytest.raises(ValueError, match="model id"):
        ModelId.parse(value)
