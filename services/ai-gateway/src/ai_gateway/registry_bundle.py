"""Prompt Registry 묶음(프롬프트·질의 사전·분류 체계·출력 형식·프로필)을 만들고 받아 푼다.

SPEC-009 R-26~R-28.

묶음은 `.tar.gz` 하나이고 설정이 그 파일 해시로 고정한다. 같은 내용이면 언제 만들어도 같은 바이트가
나오게 만들어, 해시가 곧 내용의 버전이 된다(docs ADR-0010 ⑦).

    python -m ai_gateway.registry_bundle <registry 폴더> <profiles 폴더> <출력 파일>
"""

import gzip
import hashlib
import io
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path

from vlm_common.profile import ProfileError
from vlm_storage.artifacts import fetch_file
from vlm_storage.storage import ObjectStorage

from ai_gateway.settings import GatewaySettings

# 묶음 안의 폴더. 이미지의 /app/registry와 같은 배치다. events는 판정 출력 형식(계약)이다.
_REGISTRY_FOLDERS = ("prompts", "query", "events", "taxonomy")
_PROFILES_FOLDER = "profiles"
# 다 푼 뒤에만 남겨, 풀다 멈춘 폴더를 캐시로 쓰지 않게 한다.
_COMPLETE_MARKER = ".complete"


@dataclass(frozen=True, slots=True)
class RegistrySources:
    """기동할 때 읽을 Registry 폴더와 프로필 파일."""

    registry_dir: Path
    profile_path: Path


def build_bundle(registry_dir: Path, profiles_dir: Path, output: Path) -> str:
    """Registry 폴더와 프로필 폴더를 묶어 `output`에 쓰고 파일 sha256을 돌려준다.

    파일 순서·시각·소유자를 고정해 같은 내용이면 같은 바이트가 된다.
    """
    files = [
        (path, path.relative_to(registry_dir).as_posix())
        for folder in _REGISTRY_FOLDERS
        for path in (registry_dir / folder).rglob("*")
        if path.is_file()
    ] + [
        (path, f"{_PROFILES_FOLDER}/{path.name}")
        for path in profiles_dir.glob("*.toml")
        if path.is_file()
    ]
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, filename="") as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for path, name in sorted(files, key=lambda item: item[1]):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    output.write_bytes(buffer.getvalue())
    return hashlib.sha256(buffer.getvalue()).hexdigest()


def open_bundle(storage: ObjectStorage, key: str, sha256: str, cache_dir: Path) -> Path:
    """묶음을 받아 해시를 확인하고 푼 폴더를 돌려준다. 이미 풀었으면 그 폴더를 쓴다(R-28).

    Raises:
        ArtifactError: 묶음이 없거나 해시가 다를 때(R-27).
    """
    root = cache_dir / sha256 / "root"
    if (root / _COMPLETE_MARKER).is_file():
        return root
    archive = fetch_file(storage, key, sha256, cache_dir)
    partial = root.with_name("root.partial")
    shutil.rmtree(partial, ignore_errors=True)
    with tarfile.open(archive, mode="r:gz") as tar:
        # data 필터는 절대 경로·상위 폴더·링크 항목을 거절한다.
        tar.extractall(partial, filter="data")
    (partial / _COMPLETE_MARKER).touch()
    shutil.rmtree(root, ignore_errors=True)
    partial.replace(root)
    return root


def resolve_sources(settings: GatewaySettings, models: ObjectStorage | None) -> RegistrySources:
    """설정에 묶음이 있으면 받아 푼 폴더를, 없으면 이미지의 Registry·프로필 경로를 돌려준다.

    Raises:
        ProfileError: 묶음 키·해시 중 하나만 있거나, 묶음인데 프로필 경로가 묶음 밖이거나, 모델 버킷
            설정이 없을 때(R-14).
        ArtifactError: 묶음을 받지 못했거나 해시가 다를 때(R-27).
    """
    key, sha256 = settings.registry_bundle, settings.registry_bundle_sha256
    if key is None and sha256 is None:
        return RegistrySources(settings.registry_dir, settings.profile_path)
    if key is None or sha256 is None:
        raise ProfileError("registry_bundle and registry_bundle_sha256 must be set together")
    if settings.profile_path.is_absolute():
        raise ProfileError("profile_path must be relative inside the registry bundle")
    if models is None:
        raise ProfileError("registry bundle requires the models bucket setting")
    root = open_bundle(models, key, sha256, settings.model_cache_dir / "registry")
    profile_path = (root / settings.profile_path).resolve()
    if not profile_path.is_relative_to(root.resolve()):
        raise ProfileError("profile_path must be relative inside the registry bundle")
    return RegistrySources(root, profile_path)


def main(argv: list[str]) -> None:
    """묶음을 만들고 sha256을 표준 출력에 쓴다. 로컬 업로드 스크립트와 CI가 쓴다."""
    registry_dir, profiles_dir, output = (Path(arg) for arg in argv)
    sys.stdout.write(build_bundle(registry_dir, profiles_dir, output) + "\n")


if __name__ == "__main__":
    main(sys.argv[1:])
