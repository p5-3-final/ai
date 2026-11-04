"""모델 산출물을 manifest로 받는다(docs ADR-0010 ②).

프로필은 `models/<종류>/<버전>/manifest.json`의 키와 manifest 파일의 sha256을 가리킨다. manifest가
같은 폴더의 파일과 각 sha256을 고정하므로, 해시 두 단계로 받은 파일 전체가 프로필에 묶인다.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pydantic import ValidationError
from vlm_common.errors import VlmError
from vlm_contracts.models import ManifestFile, ModelManifest

from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

_CHUNK = 1 << 20
_MANIFEST = "manifest.json"


class ArtifactError(VlmError):
    """manifest나 파일을 받지 못했거나 체크섬이 다르다."""

    code = "STORAGE_ARTIFACT_INVALID"


@dataclass(frozen=True, slots=True)
class FetchedModel:
    """체크섬을 확인한 manifest와 파일 이름별 로컬 경로."""

    manifest: ModelManifest
    files: dict[str, Path]


def fetch_model(
    storage: ObjectStorage, manifest_key: str, manifest_sha256: str, cache_dir: Path
) -> FetchedModel:
    """manifest와 거기 적힌 파일을 받아 체크섬을 확인한 로컬 경로를 돌려준다.

    캐시 폴더 이름을 manifest 해시로 해, 프로필이 다른 manifest를 가리키면 다른 폴더가 된다.

    Args:
        storage: 모델 버킷.
        manifest_key: manifest 객체 키(프로필 값).
        manifest_sha256: 기대하는 manifest 파일 해시(소문자 16진수, 프로필 값).
        cache_dir: 로컬 캐시 폴더(Pod의 emptyDir 등).

    Raises:
        ArtifactError: manifest나 파일이 없거나, 해시가 다르거나, manifest가 계약 형식이 아닐 때.
    """
    folder = cache_dir / manifest_sha256
    cached = folder / _MANIFEST
    # Pod 재시작처럼 캐시 폴더가 남아 있으면 저장소를 읽지 않는다(SPEC-011 R-18).
    if cached.is_file() and _sha256(cached) == manifest_sha256:
        raw = cached.read_bytes()
    else:
        raw = _get_bytes(storage, manifest_key)
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ArtifactError(f"manifest sha256 mismatch for {manifest_key}")
    try:
        manifest = ModelManifest.model_validate_json(raw)
    except ValidationError as e:
        raise ArtifactError(f"invalid manifest {manifest_key}: {e.error_count()} errors") from e

    prefix = PurePosixPath(manifest_key).parent
    files = {f.name: _fetch_file(storage, str(prefix / f.name), f, folder) for f in manifest.files}
    # 파일을 모두 확인한 뒤에 manifest를 남겨, 캐시에 manifest가 있으면 받기가 끝난 것이 되게 한다.
    if not cached.is_file():
        cached.write_bytes(raw)
    return FetchedModel(manifest=manifest, files=files)


def fetch_file(storage: ObjectStorage, key: str, sha256: str, cache_dir: Path) -> Path:
    """manifest 없이 해시 하나로 고정한 파일(Registry 묶음 등)을 받아 확인한 로컬 경로를 돌려준다.

    Args:
        storage: 객체 저장소.
        key: 객체 키(설정 값).
        sha256: 기대하는 파일 해시(소문자 16진수, 설정 값).
        cache_dir: 로컬 캐시 폴더. 파일은 `<cache_dir>/<sha256>/<키의 파일 이름>`에 둔다.

    Raises:
        ArtifactError: 파일이 없거나 해시가 다를 때.
    """
    name = PurePosixPath(key).name
    return _fetch(storage, key, name, sha256, cache_dir / sha256)


def _fetch_file(storage: ObjectStorage, key: str, expected: ManifestFile, folder: Path) -> Path:
    return _fetch(storage, key, expected.name, expected.sha256, folder)


def _fetch(storage: ObjectStorage, key: str, name: str, sha256: str, folder: Path) -> Path:
    target = folder / name
    if target.is_file() and _sha256(target) == sha256:
        return target
    folder.mkdir(parents=True, exist_ok=True)
    # 받다가 멈춘 파일이 캐시로 쓰이지 않도록 임시 이름으로 받아 확인한 뒤 옮긴다.
    partial = target.with_name(target.name + ".partial")
    try:
        storage.get_file(key, partial)
    except ObjectNotFoundError as e:
        raise ArtifactError(f"file not found: {key}") from e
    if _sha256(partial) != sha256:
        partial.unlink()
        raise ArtifactError(f"sha256 mismatch for {key}")
    partial.replace(target)
    return target


def _get_bytes(storage: ObjectStorage, key: str) -> bytes:
    try:
        return storage.get_bytes(key)
    except ObjectNotFoundError as e:
        raise ArtifactError(f"manifest not found: {key}") from e


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
