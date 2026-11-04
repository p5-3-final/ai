"""모델 산출물 manifest Pydantic 모델.

원본은 docs 레포 `service-contracts/models/manifest.schema.json`이고(ADR-0010 ②), 계약 테스트가 둘이
같은지 확인한다. JSON 필드 이름은 camelCase, 파이썬 속성은 snake_case다.
"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

type ModelKind = Literal["image_text_encoder", "detector", "predictor"]
# 분류 체계 v2 `className`(service-contracts/taxonomy/v2/taxonomy.schema.json)
type ClassName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class _Contract(BaseModel):
    # 받은 manifest는 체크섬으로 고정한 내용이라 고쳐 쓰지 않는다. 모르는 필드는 계약 위반이다.
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class ManifestFile(_Contract):
    """manifest와 같은 폴더의 파일 하나."""

    # 하위 폴더를 허용하지 않아 manifest 폴더 밖의 키나 캐시 경로를 가리킬 수 없다.
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=128)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=1)


class PublicSource(_Contract):
    """공개 가중치를 그대로 쓴 것."""

    type: Literal["public"]
    origin: str = Field(min_length=1, max_length=200)


class TrainedSource(_Contract):
    """팀이 학습·파인튜닝한 것."""

    type: Literal["trained"]
    dataset: str = Field(min_length=1, max_length=128)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    base_model_id: str | None = Field(default=None, max_length=128)


class ModelManifest(_Contract):
    """모델 저장소 `models/<종류>/<버전>/manifest.json`."""

    kind: ModelKind
    model_id: str = Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*(:[A-Za-z0-9][A-Za-z0-9._-]*){2}$", max_length=128
    )
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=64)
    files: list[ManifestFile] = Field(min_length=1)
    dimension: int | None = Field(default=None, ge=1)
    classes: list[ClassName] | None = Field(default=None, min_length=1)
    source: Annotated[PublicSource | TrainedSource, Field(discriminator="type")]
    license: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        if self.kind == "image_text_encoder" and self.dimension is None:
            raise ValueError("image_text_encoder manifest requires dimension")
        if self.kind == "detector" and self.classes is None:
            raise ValueError("detector manifest requires classes")
        return self
