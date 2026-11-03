"""블러 사본 이미지 읽기."""

import asyncio
from pathlib import PurePosixPath

from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from ai_gateway.adapters.base import ImageInput
from ai_gateway.errors import ImageNotFoundError, ImagePathForbiddenError

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ImageReader:
    """저장소에서 이미지를 읽는다. boto3가 동기라 스레드에서 병렬로 읽는다."""

    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    async def read(self, keys: list[str]) -> list[ImageInput]:
        """요청 순서대로 읽는다.

        Raises:
            ImagePathForbiddenError: 이미지 형식이 아닌 경로.
            ImageNotFoundError: 저장소에 없는 경로(SPEC-009 R-11).
        """
        return list(await asyncio.gather(*(self._read_one(k) for k in keys)))

    async def _read_one(self, key: str) -> ImageInput:
        media_type = _MEDIA_TYPES.get(PurePosixPath(key).suffix.lower())
        if media_type is None:
            raise ImagePathForbiddenError(f"not an image path: {key}")
        try:
            data = await asyncio.to_thread(self._storage.get_bytes, key)
        except ObjectNotFoundError as e:
            raise ImageNotFoundError(f"image not found: {key}") from e
        return ImageInput(data=data, media_type=media_type)
