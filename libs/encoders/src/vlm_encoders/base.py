"""이미지·질의 인코더 인터페이스(docs ADR-0008 `ImageTextEncoder`).

ai-gateway의 질의 벡터와 frame-encoder의 구간·크롭 벡터가 같은 구현을 써야 검색에서 비교할 수 있다.
"""

from typing import Protocol

from vlm_common.errors import VlmError

type Vector = list[float]


class EncoderLoadError(VlmError):
    """가중치를 받거나 읽지 못했거나, 출력이 프로필과 다르다. 기동을 멈춰야 한다."""

    code = "ENCODER_LOAD_FAILED"


class ImageDecodeError(VlmError):
    """이미지 바이트를 디코딩할 수 없다."""

    code = "ENCODER_IMAGE_UNREADABLE"


class ImageTextEncoder(Protocol):
    """문장과 이미지를 같은 공간의 벡터로 바꾼다.

    호출은 블로킹이고 CPU를 많이 쓴다. async 핸들러에서는 스레드에서 부른다. 돌려주는 벡터는
    모두 길이(L2 노름) 1이고, 같은 입력이면 같은 벡터다.
    """

    @property
    def model_id(self) -> str:
        """벡터와 함께 기록하는 `embedding_model` 값(프로필의 모델 ID)."""
        ...

    @property
    def dimension(self) -> int:
        """벡터 차원."""
        ...

    def encode_texts(self, texts: list[str]) -> list[Vector]:
        """문장마다 벡터를 입력 순서대로 돌려준다."""
        ...

    def encode_images(self, images: list[bytes]) -> list[Vector]:
        """이미지(인코딩된 파일 바이트)마다 벡터를 입력 순서대로 돌려준다.

        Raises:
            ImageDecodeError: 디코딩할 수 없는 이미지가 있을 때.
        """
        ...
