"""블러 사본 이미지 읽기."""

import asyncio
import re
from pathlib import PurePosixPath
from uuid import UUID

from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from ai_gateway.adapters.base import ImageInput
from ai_gateway.errors import ImageNotFoundError, ImagePathForbiddenError
from ai_gateway.profile import SOURCE_ID_PLACEHOLDER

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}


class ImagePolicy:
    """외부 모델로 보내도 되는 경로인지 검사한다(SPEC-009 R-9, R-10).

    허용 목록 방식이라 원본·정규화 사본·HLS처럼 패턴에 없는 경로는 모두 막힌다. 새 경로 규칙이
    생겨도 프로필에 넣기 전에는 새어 나가지 않는다.
    """

    def __init__(self, patterns: list[str]) -> None:
        self._patterns = patterns

    def check(self, source_id: UUID, keys: list[str]) -> None:
        """하나라도 어긋나면 요청 전체를 거절한다.

        Raises:
            ImagePathForbiddenError: 영상 폴더 밖이거나 허용 패턴에 맞지 않는 경로가 있을 때.
        """
        folder = f"sources/{source_id}/"
        allowed = [
            re.compile(p.replace(SOURCE_ID_PLACEHOLDER, re.escape(str(source_id))))
            for p in self._patterns
        ]
        for key in keys:
            # 패턴이 영상 ID 자리 없이 쓰여도 다른 영상의 이미지를 보내지 못하게 따로 막는다.
            if not key.startswith(folder) or ".." in key.split("/"):
                raise ImagePathForbiddenError(f"image path is outside the source folder: {key}")
            if not any(p.fullmatch(key) for p in allowed):
                raise ImagePathForbiddenError(f"only blurred copies can be sent: {key}")


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
