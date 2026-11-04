"""인코더 가중치를 모델 저장소의 manifest로 받는다(docs overview "모델과 플랫폼 교체", ADR-0010 ②).

가중치는 이미지에 넣지 않고 기동할 때 받는다(SPEC-011 NFR-6). 체크섬은 manifest가 고정하고, 프로필의
모델 ID·차원은 기대값이라 manifest와 다르면 로드하지 않는다.
"""

from pathlib import Path

from vlm_storage.artifacts import ArtifactError, fetch_model
from vlm_storage.storage import ObjectStorage

from vlm_encoders.base import EncoderLoadError
from vlm_encoders.model_id import ModelId


def fetch_encoder_weights(
    storage: ObjectStorage,
    manifest_key: str,
    manifest_sha256: str,
    model_id: str,
    dimension: int,
    cache_dir: Path,
) -> Path:
    """manifest와 가중치를 받아 프로필과 맞는지 확인한 가중치 파일 경로를 돌려준다.

    Args:
        storage: 모델 버킷.
        manifest_key: manifest 객체 키(프로필 값).
        manifest_sha256: 기대하는 manifest 파일 해시(프로필 값).
        model_id: 프로필의 인코더 모델 ID. 집계를 뺀 앞부분이 manifest의 모델 ID여야 한다.
        dimension: 프로필의 출력 차원.
        cache_dir: 로컬 캐시 폴더(Pod의 emptyDir 등).

    Raises:
        EncoderLoadError: 받지 못했거나 해시가 다르거나, manifest가 프로필과 맞지 않을 때
            (SPEC-011 R-16).
    """
    try:
        fetched = fetch_model(storage, manifest_key, manifest_sha256, cache_dir)
    except ArtifactError as e:
        raise EncoderLoadError(str(e)) from e
    manifest = fetched.manifest
    if manifest.kind != "image_text_encoder":
        raise EncoderLoadError(f"manifest kind {manifest.kind} is not image_text_encoder")
    expected = ModelId.parse(model_id)
    weights_id = f"{expected.library}:{expected.architecture}:{expected.weights}"
    if manifest.model_id != weights_id:
        raise EncoderLoadError(f"manifest model id {manifest.model_id} != profile {weights_id}")
    if manifest.dimension != dimension:
        raise EncoderLoadError(f"manifest dimension {manifest.dimension} != profile {dimension}")
    if len(fetched.files) != 1:
        raise EncoderLoadError("encoder manifest must list exactly one weights file")
    return next(iter(fetched.files.values()))
