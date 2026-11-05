"""객체 저장소 인터페이스. 서비스 코드는 이 프로토콜만 쓴다(boto3는 `s3` 구현에만 둔다)."""

from pathlib import Path
from typing import Protocol

from vlm_common.errors import VlmError


class ObjectNotFoundError(VlmError):
    """요청한 키의 객체가 없다."""

    code = "STORAGE_OBJECT_NOT_FOUND"


class ObjectStorage(Protocol):
    """버킷 하나를 키로 읽고 쓰는 저장소.

    호출은 블로킹이다. async 핸들러에서는 `asyncio.to_thread`로 부른다. 같은 키에 다시 쓰면
    덮어쓰므로 결정적 경로에 쓰는 재처리는 같은 결과를 남긴다.
    """

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        """`data`를 `key`에 쓴다."""
        ...

    def put_bytes_if_absent(self, key: str, data: bytes, content_type: str) -> bool:
        """키가 없을 때만 원자적으로 쓴다. 이미 있으면 변경 없이 False다."""
        ...

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        """로컬 파일을 `key`에 올린다. 큰 파일은 구현이 나눠 올린다."""
        ...

    def get_bytes(self, key: str) -> bytes:
        """`key`의 내용을 읽는다. 없으면 `ObjectNotFoundError`."""
        ...

    def get_file(self, key: str, path: Path) -> None:
        """`key`를 `path`로 내려받는다. 상위 폴더가 없으면 만든다. 없으면 `ObjectNotFoundError`."""
        ...

    def exists(self, key: str) -> bool:
        """`key`에 객체가 있으면 True."""
        ...

    def delete_prefix(self, prefix: str) -> int:
        """`prefix`로 시작하는 객체를 모두 지우고 지운 수를 돌려준다(영상 삭제 전파용)."""
        ...
