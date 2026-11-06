"""프로필의 인코더 설정과 로드(SPEC-011 R-15~R-17, SPEC-019 R-9).

ai-gateway(질의 벡터)와 frame-encoder(구간·크롭 벡터)가 같은 검증과 로드를 거쳐야 두 벡터가 같은
공간에 있다. 그래서 두 서비스가 이 모듈을 함께 쓴다.
"""

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator
from vlm_common.profile import Profile, ProfileError
from vlm_storage.storage import ObjectStorage

from vlm_encoders.base import ImageTextEncoder
from vlm_encoders.mock import MockEncoder
from vlm_encoders.model_id import ModelId
from vlm_encoders.open_clip import OpenClipEncoder
from vlm_encoders.weights import fetch_encoder_weights

type EncoderKind = Literal["open_clip", "mock"]


class EncoderProfile(Profile):
    """이미지·질의 인코더 프로필(`[encoder]`)."""

    kind: EncoderKind
    # ai-gateway와 frame-encoder가 같은 문자열이어야 같은 인덱스를 찾는다(SPEC-011 R-6).
    model_id: str
    dimension: int = Field(gt=0)
    # 모델 버킷의 manifest 키와 manifest 파일 해시(docs ADR-0010 ②). 가중치 해시는 manifest가
    # 고정한다. mock은 가중치가 없다.
    manifest: str | None = Field(
        default=None,
        pattern=r"^models/[a-z0-9][a-z0-9_-]*/[A-Za-z0-9][A-Za-z0-9._-]*/manifest\.json$",
    )
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _weights(self) -> Self:
        ModelId.parse(self.model_id)
        if self.kind == "open_clip" and (self.manifest is None or self.manifest_sha256 is None):
            raise ValueError("open_clip encoder requires manifest and manifest_sha256")
        return self


def load_encoder(
    encoder: EncoderProfile, models: ObjectStorage | None, cache_dir: Path
) -> ImageTextEncoder:
    """프로필대로 인코더를 한 번 로드한다. 블로킹이라 기동 단계에서 부른다.

    Args:
        encoder: 검증한 인코더 프로필.
        models: 모델 버킷. 설정이 없으면 None이다.
        cache_dir: 가중치 로컬 캐시 폴더.

    Raises:
        ProfileError: open_clip 인코더인데 모델 버킷 설정이 없을 때.
        EncoderLoadError: manifest·가중치를 받지 못했거나, 해시가 다르거나, manifest·로드한 모델이
            프로필과 맞지 않을 때(SPEC-011 R-16, R-17).
    """
    if encoder.kind == "mock":
        return MockEncoder(encoder.model_id, encoder.dimension)
    # open_clip 프로필 검증에서 manifest 경로·해시가 있음을 확인했다.
    if models is None or encoder.manifest is None or encoder.manifest_sha256 is None:
        raise ProfileError("open_clip encoder requires the models bucket setting")
    weights = fetch_encoder_weights(
        models,
        encoder.manifest,
        encoder.manifest_sha256,
        encoder.model_id,
        encoder.dimension,
        cache_dir,
    )
    return OpenClipEncoder.load(encoder.model_id, weights, encoder.dimension)
