"""CI·로컬 mock 프로필 전용 인코더. 운영 프로필에서 고르면 기동에 실패한다(SPEC-011 R-24)."""

import hashlib
import math

from vlm_encoders.base import Vector


class MockEncoder:
    """입력 해시로 결정적 벡터를 만든다. 의미는 없고 흐름만 확인한다."""

    def __init__(self, model_id: str, dimension: int) -> None:
        self._model_id = model_id
        self._dimension = dimension

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode_texts(self, texts: list[str]) -> list[Vector]:
        return [self._vector(b"text:" + t.encode()) for t in texts]

    def encode_images(self, images: list[bytes]) -> list[Vector]:
        return [self._vector(b"image:" + i) for i in images]

    def _vector(self, data: bytes) -> Vector:
        # 차원만큼 바이트가 필요해 카운터를 붙여 해시를 이어 붙인다.
        stream = b"".join(
            hashlib.sha256(data + i.to_bytes(4, "big")).digest()
            for i in range(math.ceil(self._dimension / 32))
        )
        raw = [b - 127.5 for b in stream[: self._dimension]]
        norm = math.hypot(*raw)
        return [x / norm for x in raw]
