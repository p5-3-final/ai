"""실제 가중치 대신 무작위로 초기화한 MobileCLIP2-S0를 파일로 저장해 쓴다(내려받기 없음)."""

import io
from pathlib import Path

import open_clip  # type: ignore[import-untyped]  # 타입 정보를 배포하지 않는다 (#19)
import pytest
import torch
from PIL import Image
from safetensors.torch import save_model
from vlm_encoders.base import EncoderLoadError, ImageDecodeError
from vlm_encoders.open_clip import OpenClipEncoder

MODEL_ID = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
DIMENSION = 512


@pytest.fixture(scope="session")
def weights(tmp_path_factory: pytest.TempPathFactory) -> Path:
    torch.manual_seed(0)
    model = open_clip.create_model("MobileCLIP2-S0", pretrained=None)
    path = tmp_path_factory.mktemp("weights") / "open_clip_model.safetensors"
    save_model(model, str(path))
    return path


def test_spec011_r17_loads_when_dimension_matches(weights: Path) -> None:
    """[SPEC-011 R-17] 텍스트·이미지 출력 차원이 프로필과 같으면 로드된다."""
    encoder = OpenClipEncoder.load(MODEL_ID, weights, DIMENSION)

    assert encoder.model_id == MODEL_ID
    assert encoder.dimension == DIMENSION


def test_spec011_r17_rejects_dimension_mismatch(weights: Path) -> None:
    """[SPEC-011 R-17] 출력 차원이 프로필과 다르면 로드에 실패한다."""
    with pytest.raises(EncoderLoadError, match="dimension"):
        OpenClipEncoder.load(MODEL_ID, weights, 256)


@pytest.mark.parametrize(
    "model_id",
    ["timm:MobileCLIP2-S0:dfndr2b:mean-4", "open_clip:MobileCLIP2-S0:unknown:mean-4"],
)
def test_spec011_r16_rejects_unsupported_model_id(weights: Path, model_id: str) -> None:
    """[SPEC-011 R-16] open_clip이 아니거나 모르는 가중치 이름이면 로드에 실패한다."""
    with pytest.raises(EncoderLoadError):
        OpenClipEncoder.load(model_id, weights, DIMENSION)


def test_spec011_r16_rejects_unreadable_weights(tmp_path: Path) -> None:
    """[SPEC-011 R-16] 가중치 파일을 읽을 수 없으면 로드에 실패한다."""
    broken = tmp_path / "open_clip_model.safetensors"
    broken.write_bytes(b"not a checkpoint")

    with pytest.raises(EncoderLoadError):
        OpenClipEncoder.load(MODEL_ID, broken, DIMENSION)


def test_spec011_r6_image_preprocessing_matches_official_pretrained_config(weights: Path) -> None:
    """[SPEC-011 R-6] 로컬 파일로 읽어도 공식 pretrained 설정의 전처리로 이미지 벡터를 만든다.

    전처리가 다르면 frame-encoder·공식 가중치와 다른 공간의 벡터가 된다.
    """
    cfg = open_clip.get_pretrained_cfg("MobileCLIP2-S0", "dfndr2b")
    model = open_clip.create_model("MobileCLIP2-S0", pretrained=str(weights)).eval()
    transform = open_clip.image_transform(
        model.visual.image_size,
        is_train=False,
        mean=cfg["mean"],
        std=cfg["std"],
        resize_mode=cfg["resize_mode"],
        interpolation=cfg["interpolation"],
    )
    data = _jpeg(640, 360)
    with torch.inference_mode():
        expected = model.encode_image(transform(Image.open(io.BytesIO(data))).unsqueeze(0))
        expected = torch.nn.functional.normalize(expected, dim=-1)[0]

    actual = OpenClipEncoder.load(MODEL_ID, weights, DIMENSION).encode_images([data])[0]

    assert torch.allclose(torch.tensor(actual), expected, atol=1e-5)


def _jpeg(width: int, height: int, color: tuple[int, int, int] = (10, 200, 30)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def encoder(weights: Path) -> OpenClipEncoder:
    return OpenClipEncoder.load(MODEL_ID, weights, DIMENSION)


def test_spec011_r4_vectors_have_unit_length(encoder: OpenClipEncoder) -> None:
    """[SPEC-011 R-4] 문장·이미지 벡터는 입력 순서대로, 프로필 차원이고 길이 1이다."""
    vectors = [
        *encoder.encode_texts(["a forklift", "a person"]),
        *encoder.encode_images([_jpeg(640, 360), _jpeg(320, 480, (200, 10, 10))]),
    ]

    assert len(vectors) == 4
    assert all(len(v) == DIMENSION for v in vectors)
    assert torch.allclose(torch.tensor(vectors).norm(dim=-1), torch.ones(4), atol=1e-5)


def test_spec011_r5_same_input_gives_same_vector(encoder: OpenClipEncoder) -> None:
    """[SPEC-011 R-5] 같은 입력이면 코사인 유사도 0.9999 이상이고, 순서는 입력을 따른다."""
    batch = encoder.encode_texts(["a forklift", "a person"])
    forklift = encoder.encode_texts(["a forklift"])[0]
    person = encoder.encode_texts(["a person"])[0]
    image = _jpeg(640, 360)

    assert _cosine(batch[0], forklift) >= 0.9999
    assert _cosine(batch[1], person) >= 0.9999
    assert _cosine(forklift, person) < 0.9999
    assert _cosine(encoder.encode_images([image])[0], encoder.encode_images([image])[0]) >= 0.9999


class _PixelMeanModel:
    # 무작위 초기화 모델은 이미지가 달라도 거의 같은 벡터를 내서 순서를 구분하지 못한다.
    # 색 채널 평균을 벡터로 내는 가짜 모델로 입력 순서가 유지되는지 본다.
    def encode_text(self, text: torch.Tensor) -> torch.Tensor:
        return text.float()

    def encode_image(self, image: torch.Tensor) -> torch.Tensor:
        return image.mean(dim=(2, 3)) + 0.01


def _pixels(image: Image.Image) -> torch.Tensor:
    width, height = image.size
    flat = torch.frombuffer(bytearray(image.tobytes()), dtype=torch.uint8)
    return flat.reshape(height, width, 3).permute(2, 0, 1).float()


def test_spec011_r5_image_vectors_follow_input_order() -> None:
    """[SPEC-011 R-5] 이미지 벡터는 입력 순서대로 돌려준다."""
    encoder = OpenClipEncoder(
        MODEL_ID,
        3,
        _PixelMeanModel(),
        _pixels,
        lambda texts: torch.ones(len(texts), 3),
    )
    red, green = _jpeg(8, 8, (255, 0, 0)), _jpeg(8, 8, (0, 255, 0))

    vectors = encoder.encode_images([red, green])

    assert vectors[0][0] > vectors[0][1]
    assert vectors[1][1] > vectors[1][0]


def test_spec011_r12_rejects_undecodable_image(encoder: OpenClipEncoder) -> None:
    """[SPEC-011 R-12] 디코딩할 수 없는 이미지면 ImageDecodeError다."""
    with pytest.raises(ImageDecodeError):
        encoder.encode_images([_jpeg(64, 64), b"not an image"])
    with pytest.raises(ImageDecodeError):
        encoder.encode_images([_jpeg(64, 64)[:100]])


def _cosine(a: list[float], b: list[float]) -> float:
    return float(torch.nn.functional.cosine_similarity(torch.tensor(a), torch.tensor(b), dim=0))
