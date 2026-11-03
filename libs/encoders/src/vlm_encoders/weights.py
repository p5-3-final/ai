"""모델 가중치를 모델 저장소에서 로컬 캐시로 받는다(docs overview "모델과 플랫폼 교체").

가중치는 이미지에 넣지 않고 기동할 때 받는다(SPEC-011 NFR-6). 받은 파일은 프로필의 sha256과
맞을 때만 쓴다.
"""

import hashlib
from pathlib import Path

from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from vlm_encoders.base import EncoderLoadError

_CHUNK = 1 << 20


def fetch_weights(storage: ObjectStorage, key: str, sha256: str, cache_dir: Path) -> Path:
    """가중치 파일을 받아 해시를 확인한 로컬 경로를 돌려준다.

    캐시 경로에 해시를 넣어, 프로필이 다른 가중치를 가리키면 다른 파일이 되게 한다.

    Args:
        storage: 모델 버킷.
        key: 가중치 객체 키(프로필 값).
        sha256: 기대하는 파일 해시(소문자 16진수, 프로필 값).
        cache_dir: 로컬 캐시 폴더(Pod의 emptyDir 등).

    Raises:
        EncoderLoadError: 객체가 없거나 해시가 다를 때.
    """
    target = cache_dir / sha256 / Path(key).name
    # Pod 재시작처럼 캐시 폴더가 남아 있으면 다시 받지 않는다(SPEC-011 R-18).
    if target.is_file() and _sha256(target) == sha256:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    # 받다가 멈춘 파일이 캐시로 쓰이지 않도록 임시 이름으로 받아 확인한 뒤 옮긴다.
    partial = target.with_name(target.name + ".partial")
    try:
        storage.get_file(key, partial)
    except ObjectNotFoundError as e:
        raise EncoderLoadError(f"weights not found: {key}") from e
    actual = _sha256(partial)
    if actual != sha256:
        partial.unlink()
        raise EncoderLoadError(f"weights sha256 mismatch for {key}: {actual}")
    partial.replace(target)
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
