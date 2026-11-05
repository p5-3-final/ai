"""ai-gateway 프로필(Git으로 관리하는 TOML).

모델·공급자·한도를 코드가 아니라 프로필로 바꾸게 한다(docs ADR-0008, SPEC-009 US-3).
"""

import re
from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator
from vlm_common.profile import Profile, ProfileError
from vlm_encoders.model_id import ModelId

AdapterKind = Literal["openai", "openai_compatible", "mock"]
EncoderKind = Literal["open_clip", "mock"]

# `/v1/parse`가 쓰는 Prompt Registry taskType. 설정은 [tasks]가 아니라 [parse]에 있다(SPEC-012).
QUERY_PARSE_TASK = "query_parse"

# 경로 패턴 안에서 요청의 sourceId로 바꿔 넣는 자리.
SOURCE_ID_PLACEHOLDER = "{source_id}"


class CallersProfile(Profile):
    # 호출 로그의 호출자 이름이자 X-Caller 헤더 값(계약 pattern과 같다).
    allowed: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _names(self) -> Self:
        for name in self.allowed:
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", name):
                raise ValueError(f"invalid caller name: {name!r}")
        return self


def _check_patterns(patterns: list[str]) -> None:
    for pattern in patterns:
        try:
            re.compile(pattern.replace(SOURCE_ID_PLACEHOLDER, "x"))
        except re.error as e:
            raise ValueError(f"invalid image pattern {pattern!r}: {e}") from e


class ImagesProfile(Profile):
    # 블러 사본 경로만 받는다(SPEC-009 R-9). 패턴은 키 전체와 맞아야 한다.
    allowed_patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        _check_patterns(self.allowed_patterns)
        return self


class AdapterProfile(Profile):
    kind: AdapterKind
    base_url: HttpUrl | None = None

    @model_validator(mode="after")
    def _base_url(self) -> Self:
        if self.kind == "openai_compatible" and self.base_url is None:
            raise ValueError("openai_compatible adapter requires base_url")
        return self


class TaskProfile(Profile):
    """taskType 하나의 설정. `adapter`·`model`이 없으면 모델 미설정이다(SPEC-009 R-8)."""

    adapter: str | None = None
    model: str | None = None
    max_output_tokens: int = Field(gt=0)
    max_images: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)

    @model_validator(mode="after")
    def _both_or_none(self) -> Self:
        if (self.adapter is None) != (self.model is None):
            raise ValueError("adapter and model must be set together")
        return self


class EncoderProfile(Profile):
    """이미지·질의 인코더.

    없으면 `embed_visual_text`·`embed_image`가 모델 미설정이다(SPEC-011 R-14).
    """

    kind: EncoderKind
    # frame-encoder와 같은 문자열이어야 같은 인덱스를 찾는다(SPEC-011 R-6).
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


class EncoderTextTask(Profile):
    """`embed_visual_text`: 인코더 텍스트 쪽."""

    max_inputs: int = Field(gt=0)
    max_text_chars: int = Field(gt=0)


class EncoderImageTask(Profile):
    """`embed_image`: 인코더 이미지 쪽. 검색용 이미지 경로 패턴을 따로 둔다(SPEC-011 R-10)."""

    max_inputs: int = Field(gt=0)
    allowed_patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        _check_patterns(self.allowed_patterns)
        return self


class TextEmbeddingTask(Profile):
    """`embed_text`: 텍스트 임베딩 Adapter.

    `adapter`·`model`·`dimension`이 없으면 모델 미설정이다(SPEC-011 R-14).
    """

    adapter: str | None = None
    model: str | None = None
    dimension: int | None = Field(default=None, gt=0)
    max_inputs: int = Field(gt=0)
    max_text_chars: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)

    @model_validator(mode="after")
    def _all_or_none(self) -> Self:
        if len({self.adapter is None, self.model is None, self.dimension is None}) != 1:
            raise ValueError("adapter, model and dimension must be set together")
        return self


class EmbedProfile(Profile):
    """`[embed.<taskType>]`. 없는 taskType은 모델 미설정이다(SPEC-011 R-14)."""

    embed_visual_text: EncoderTextTask | None = None
    embed_image: EncoderImageTask | None = None
    embed_text: TextEmbeddingTask | None = None


class ParseProfile(Profile):
    """`/v1/parse`(SPEC-012). `adapter`·`model`이 없으면 LLM 보조 없이 사전만 쓴다(R-12)."""

    # 검색 문장 길이 상한(R-21)
    max_query_chars: int = Field(gt=0)
    adapter: str | None = None
    model: str | None = None
    max_output_tokens: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)

    @model_validator(mode="after")
    def _both_or_none(self) -> Self:
        if (self.adapter is None) != (self.model is None):
            raise ValueError("adapter and model must be set together")
        return self


class GatewayProfile(Profile):
    callers: CallersProfile
    images: ImagesProfile
    adapters: dict[str, AdapterProfile] = Field(default_factory=dict)
    tasks: dict[str, TaskProfile]
    encoder: EncoderProfile | None = None
    embed: EmbedProfile = Field(default_factory=EmbedProfile)
    parse: ParseProfile

    @model_validator(mode="after")
    def _adapter_refs(self) -> Self:
        refs = [(name, task.adapter) for name, task in self.tasks.items()]
        if self.embed.embed_text is not None:
            refs.append(("embed_text", self.embed.embed_text.adapter))
        refs.append(("parse", self.parse.adapter))
        for name, adapter in refs:
            if adapter is not None and adapter not in self.adapters:
                raise ValueError(f"task {name} refers to unknown adapter {adapter!r}")
        return self


def check_profile(profile: GatewayProfile, *, allow_mock: bool, has_openai_key: bool) -> None:
    """환경과 맞춰 봐야 하는 규칙을 검사한다. 프로필 파일만으로는 알 수 없는 것들이다.

    Raises:
        ProfileError: mock 허용 없이 mock Adapter·인코더를 쓰거나(SPEC-009 R-15, SPEC-011 R-24,
            SPEC-012 R-28),
            API 키 없이 openai Adapter를 쓸 때.
    """
    if profile.encoder is not None and profile.encoder.kind == "mock" and not allow_mock:
        raise ProfileError("encoder is mock but mock is not allowed")
    used = {t.adapter for t in profile.tasks.values() if t.adapter is not None}
    if profile.embed.embed_text is not None and profile.embed.embed_text.adapter is not None:
        used.add(profile.embed.embed_text.adapter)
    if profile.parse.adapter is not None:
        used.add(profile.parse.adapter)
    for name in sorted(used):
        kind = profile.adapters[name].kind
        if kind == "mock" and not allow_mock:
            raise ProfileError(f"adapter {name} is mock but mock is not allowed")
        if kind == "openai" and not has_openai_key:
            raise ProfileError(f"adapter {name} is openai but no API key is set")
