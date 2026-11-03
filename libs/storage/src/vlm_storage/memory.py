"""단위 테스트용 메모리 저장소. 운영 코드에서 쓰지 않는다."""

from pathlib import Path

from vlm_storage.storage import ObjectNotFoundError


class InMemoryStorage:
    """`ObjectStorage`를 dict로 구현한다. S3 구현과 같은 테스트 묶음으로 동작을 맞춘다."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[bytes, str]] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        self._objects[key] = (data, content_type)

    def put_file(self, key: str, path: Path, content_type: str) -> None:
        self.put_bytes(key, path.read_bytes(), content_type)

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._objects[key][0]
        except KeyError as e:
            raise ObjectNotFoundError(f"object not found: {key}") from e

    def get_file(self, key: str, path: Path) -> None:
        data = self.get_bytes(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return key in self._objects

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self._objects if k.startswith(prefix)]
        for key in keys:
            del self._objects[key]
        return len(keys)
