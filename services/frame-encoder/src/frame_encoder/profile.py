"""frame-encoder 프로필(TOML). 인코더·묶음 크기·프로토타입은 코드가 아니라 이 값으로 바꾼다.

프로필이 틀리면 기동에 실패한다(SPEC-019 R-9, R-15). 잘못된 기준으로 후보를 내기보다 멈추는 편이
안전하다.
"""

from typing import Annotated

from pydantic import Field, StringConstraints
from vlm_common.profile import Profile
from vlm_contracts.events import DetectableEventType
from vlm_encoders.loader import EncoderProfile

type _Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EncodingProfile(Profile):
    """인코딩 설정(`[encoding]`)."""

    # 인코더에 한 번에 넣는 이미지 수. CPU 메모리와 처리 속도를 맞바꾼다.
    batch_size: int = Field(default=16, ge=1, le=256)


class PrototypeType(Profile):
    """유형 하나의 프로토타입(`[prototypes.types.<유형>]`)."""

    texts: list[_Text] = Field(min_length=1)
    # 코사인 유사도 범위(-1~1). -1이면 모든 구간이 후보가 되어 의미가 없다(SPEC-019 R-15).
    threshold: float = Field(gt=-1, le=1)


class PrototypesProfile(Profile):
    """텍스트 프로토타입 후보 설정(`[prototypes]`)."""

    # event.candidate.vector의 prototypeVersion. 문장이나 임계값을 바꾸면 올린다.
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    # 키는 분류 체계 v2의 탐지 가능 유형만 받는다. `other`는 후보 유형이 아니다.
    types: dict[DetectableEventType, PrototypeType] = Field(min_length=1)


class FrameEncoderProfile(Profile):
    """frame-encoder 프로필 전체."""

    encoder: EncoderProfile
    encoding: EncodingProfile = EncodingProfile()
    # 없으면 프로토타입 후보를 만들지 않는다(SPEC-019 R-13).
    prototypes: PrototypesProfile | None = None
