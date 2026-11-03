"""open_clip 인코더(기본 MobileCLIP2-S0).

open_clip·torch는 이 모듈에서만 import한다(SPEC-011 NFR-4).
"""

import io
import pickle
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import open_clip  # type: ignore[import-untyped]  # 타입 정보를 배포하지 않는다 (#19)
import torch
from PIL import Image, UnidentifiedImageError
from safetensors import SafetensorError

from vlm_encoders.base import EncoderLoadError, ImageDecodeError, Vector
from vlm_encoders.model_id import ModelId

_LIBRARY = "open_clip"


class _ClipModel(Protocol):
    # open_clip이 타입 정보를 주지 않아(#19) 이 모듈이 쓰는 메서드만 적는다.
    def encode_text(self, text: torch.Tensor) -> torch.Tensor: ...

    def encode_image(self, image: torch.Tensor) -> torch.Tensor: ...


class OpenClipEncoder:
    """open_clip 모델의 텍스트·이미지 쪽으로 같은 공간의 단위 벡터를 만든다.

    `load`로만 만든다. 모델은 eval 모드로 한 번 로드하고 요청마다 다시 읽지 않는다.
    """

    def __init__(
        self,
        model_id: str,
        dimension: int,
        model: _ClipModel,
        preprocess: Callable[[Image.Image], torch.Tensor],
        tokenize: Callable[[list[str]], torch.Tensor],
    ) -> None:
        self._model_id = model_id
        self._dimension = dimension
        self._model = model
        self._preprocess = preprocess
        self._tokenize = tokenize

    @classmethod
    def load(cls, model_id: str, weights: Path, dimension: int) -> "OpenClipEncoder":
        """가중치 파일로 모델을 로드하고 출력 차원을 확인한다.

        Args:
            model_id: 프로필의 모델 ID(`open_clip:<아키텍처>:<가중치 이름>:<집계>`).
            weights: 해시를 확인한 로컬 가중치 파일.
            dimension: 프로필의 벡터 차원.

        Raises:
            EncoderLoadError: open_clip 모델이 아니거나, 가중치를 읽지 못했거나,
                출력 차원이 `dimension`과 다를 때(SPEC-011 R-16, R-17).
        """
        parsed = _parse(model_id)
        # 로컬 파일로 읽으면 open_clip이 전처리 값을 채우지 못한다. 공식 pretrained 설정에서 가져와
        # 넘겨야 frame-encoder·공식 가중치와 같은 공간의 벡터가 된다(SPEC-011 R-6).
        cfg = open_clip.get_pretrained_cfg(parsed.architecture, parsed.weights)
        if not cfg:
            raise EncoderLoadError(f"unknown open_clip weights: {model_id}")
        try:
            model, _, preprocess = open_clip.create_model_and_transforms(
                parsed.architecture,
                pretrained=str(weights),
                image_mean=cfg["mean"],
                image_std=cfg["std"],
                image_interpolation=cfg["interpolation"],
                image_resize_mode=cfg["resize_mode"],
            )
        # 형식이 깨진 파일은 SafetensorError(.bin이면 UnpicklingError), 키가 다르거나 파일이 없으면
        # RuntimeError다.
        except (SafetensorError, pickle.UnpicklingError, RuntimeError, OSError) as e:
            raise EncoderLoadError(f"failed to load {model_id}: {e}") from e
        model.eval()
        encoder = cls(
            model_id, dimension, model, preprocess, open_clip.get_tokenizer(parsed.architecture)
        )
        encoder._check_dimension()
        return encoder

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode_texts(self, texts: list[str]) -> list[Vector]:
        with torch.inference_mode():
            features = self._model.encode_text(self._tokenize(texts))
        return _unit_rows(features)

    def encode_images(self, images: list[bytes]) -> list[Vector]:
        batch = torch.stack([self._preprocess(_decode(data)) for data in images])
        with torch.inference_mode():
            features = self._model.encode_image(batch)
        return _unit_rows(features)

    def _check_dimension(self) -> None:
        text = len(self.encode_texts(["dimension check"])[0])
        image = len(self.encode_images([_blank_jpeg()])[0])
        if text != self._dimension or image != self._dimension:
            raise EncoderLoadError(
                f"{self._model_id} output dimension text={text} image={image},"
                f" profile={self._dimension}"
            )


def _parse(model_id: str) -> ModelId:
    try:
        parsed = ModelId.parse(model_id)
    except ValueError as e:
        raise EncoderLoadError(str(e)) from e
    if parsed.library != _LIBRARY:
        raise EncoderLoadError(f"not an open_clip model: {model_id}")
    return parsed


def _decode(data: bytes) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGB")
    # Pillow는 잘린 파일을 OSError로 알린다.
    except (UnidentifiedImageError, OSError) as e:
        raise ImageDecodeError(f"cannot decode image: {e}") from e


def _unit_rows(features: torch.Tensor) -> list[Vector]:
    rows: list[Vector] = torch.nn.functional.normalize(features.float(), dim=-1).tolist()
    return rows


def _blank_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64)).save(buf, "JPEG")
    return buf.getvalue()
