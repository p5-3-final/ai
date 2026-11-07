"""ai-gateway 프로필(Git으로 관리하는 TOML).

모델·공급자·한도를 코드가 아니라 프로필로 바꾸게 한다(docs ADR-0008, SPEC-009 US-3).
"""

import re
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, model_validator
from vlm_common.profile import Profile, ProfileError
from vlm_encoders.loader import EncoderProfile

AdapterKind = Literal["openai", "openai_compatible", "mock", "predictor"]
# 외부 모델을 부르지 않고 코드로 답하는 Adapter 종류(SPEC-028 R-16).
# /v1/analyze taskType만 처리한다.
PREDICTOR_KIND = "predictor"

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


class PredictorSettings(Profile):
    """`risk_forecast` 1단계 규칙 점수의 자리값(SPEC-028 R-16). AI 담당자가 바꾼다.

    Attributes:
        horizons_sec: 예측 구간(초). 계약 `_risk-forecast` 범위(60~86400, 최대 6개)다.
        saturation_s: 활성 시간이 이 값에 이르면 진입률을 그대로 쓴다. 그보다 짧으면 비례해 줄인다.
    """

    horizons_sec: tuple[Annotated[int, Field(ge=60, le=86400)], ...] = Field(
        min_length=1, max_length=6
    )
    saturation_s: float = Field(gt=0)


class AdapterProfile(Profile):
    kind: AdapterKind
    base_url: HttpUrl | None = None
    # kind predictor일 때만 둔다.
    predictor: PredictorSettings | None = None

    @model_validator(mode="after")
    def _base_url(self) -> Self:
        if self.kind == "openai_compatible" and self.base_url is None:
            raise ValueError("openai_compatible adapter requires base_url")
        if (self.kind == PREDICTOR_KIND) != (self.predictor is not None):
            raise ValueError("predictor settings are required for, and only for, kind predictor")
        return self


class TaskProfile(Profile):
    """taskType 하나의 설정. `adapter`·`model`이 없으면 모델 미설정이다(SPEC-009 R-8)."""

    adapter: str | None = None
    model: str | None = None
    max_output_tokens: int = Field(gt=0)
    # 받는 이미지 수의 하한. 기존 taskType은 하한이 없어 기본 0이다(SPEC-024 R-11·R-12).
    min_images: int = Field(default=0, ge=0)
    max_images: int = Field(gt=0)
    timeout_s: float = Field(gt=0)
    max_retries: int = Field(ge=0)
    # 예산 예약에 쓰는 입력 토큰 예상값(이미지 포함). 정산은 공급자 토큰으로 한다(SPEC-016).
    estimated_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _both_or_none(self) -> Self:
        if (self.adapter is None) != (self.model is None):
            raise ValueError("adapter and model must be set together")
        if self.min_images > self.max_images:
            raise ValueError("min_images must not exceed max_images")
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
    # 문장 하나의 입력 토큰 예상값(SPEC-016 R-10)
    estimated_tokens_per_text: int = Field(default=0, ge=0)

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
    # 예산 예약에 쓰는 입력 토큰 예상값(SPEC-016 R-10)
    estimated_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _both_or_none(self) -> Self:
        if (self.adapter is None) != (self.model is None):
            raise ValueError("adapter and model must be set together")
        return self


class CacheProfile(Profile):
    """결과 캐시(SPEC-016 R-1, R-2). 영속 저장 결과는 보관 기간이 지나면 쓰지 않는다."""

    retention_days: int = Field(default=30, gt=0)
    # 앞단 캐시(Redis) 유지 시간
    front_ttl_s: int = Field(default=3600, gt=0)


class BudgetProfile(Profile):
    """하루(UTC) 외부 모델 비용 한도(USD). 사용자 상한이 아니라 사고를 막는 안전장치다(SPEC-016)."""

    daily_limit_usd: Decimal = Field(ge=0)
    # 호출 서비스(X-Caller)별 한도. 없는 서비스는 전체 한도만 적용한다(R-21).
    callers: dict[str, Decimal] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _non_negative(self) -> Self:
        for name, limit in self.callers.items():
            if limit < 0:
                raise ValueError(f"budget for {name} must not be negative")
        return self

    def limit_for(self, caller: str) -> Decimal | None:
        return self.callers.get(caller)


class ModelPrice(Profile):
    """100만 토큰당 USD. 단가가 없는 모델은 비용 0이다(SPEC-016 R-14)."""

    input_per_million_usd: Decimal = Field(ge=0)
    output_per_million_usd: Decimal = Field(default=Decimal(0), ge=0)


class RateLimitProfile(Profile):
    """모든 Pod를 합친 분당 한도(SPEC-016 R-15). Pod별 값은 Redis가 끊겼을 때 쓴다(R-17).

    Pod별 값이 없으면 전체 값을 쓴다.
    """

    rpm: int | None = Field(default=None, gt=0)
    tpm: int | None = Field(default=None, gt=0)
    pod_rpm: int | None = Field(default=None, gt=0)
    pod_tpm: int | None = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _pod_defaults(cls, data: object) -> object:
        # 프로필은 frozen이라 검증 뒤에 채울 수 없어 입력 단계에서 채운다.
        if isinstance(data, dict):
            data = {"pod_rpm": data.get("rpm"), "pod_tpm": data.get("tpm")} | data
        return data

    @model_validator(mode="after")
    def _some_limit(self) -> Self:
        if self.rpm is None and self.tpm is None:
            raise ValueError("rate limit needs rpm or tpm")
        return self


class GatewayProfile(Profile):
    callers: CallersProfile
    images: ImagesProfile
    adapters: dict[str, AdapterProfile] = Field(default_factory=dict)
    tasks: dict[str, TaskProfile]
    encoder: EncoderProfile | None = None
    embed: EmbedProfile = Field(default_factory=EmbedProfile)
    parse: ParseProfile
    cache: CacheProfile = Field(default_factory=CacheProfile)
    # 없으면 예산 한도가 없다. 비용은 그래도 호출 로그에 남긴다.
    budget: BudgetProfile | None = None
    # [prices.<adapter>.<model>], [rate_limits.<adapter>.<model>]
    prices: dict[str, dict[str, ModelPrice]] = Field(default_factory=dict)
    rate_limits: dict[str, dict[str, RateLimitProfile]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _adapter_refs(self) -> Self:
        # predictor는 /v1/analyze taskType만 처리한다(SPEC-028 R-16).
        others = [("parse", self.parse.adapter)]
        if self.embed.embed_text is not None:
            others.append(("embed_text", self.embed.embed_text.adapter))
        refs = [(name, task.adapter) for name, task in self.tasks.items()] + others
        for name, adapter in refs:
            if adapter is not None and adapter not in self.adapters:
                raise ValueError(f"task {name} refers to unknown adapter {adapter!r}")
        for name, adapter in others:
            if adapter is not None and self.adapters[adapter].kind == PREDICTOR_KIND:
                raise ValueError(f"{name} cannot use predictor adapter {adapter!r}")
        self._check_guards()
        return self

    def _check_guards(self) -> None:
        """단가·분당 한도·예산이 쓰는 Adapter·모델·호출 서비스를 가리키는지(SPEC-016 R-20)."""
        used = self.model_pairs()
        for section, table in (("prices", self.prices), ("rate_limits", self.rate_limits)):
            for adapter, models in table.items():
                for model in models:
                    if (adapter, model) not in used:
                        raise ValueError(f"{section} refers to unused model {adapter}/{model}")
        if self.budget is not None:
            unknown = sorted(set(self.budget.callers) - set(self.callers.allowed))
            if unknown:
                raise ValueError(f"budget refers to unknown callers {unknown}")

    def model_pairs(self) -> set[tuple[str, str]]:
        """프로필이 외부 모델로 부르는 (Adapter, 모델) 목록."""
        pairs = {(t.adapter, t.model) for t in self.tasks.values() if t.adapter and t.model}
        text = self.embed.embed_text
        if text is not None and text.adapter and text.model:
            pairs.add((text.adapter, text.model))
        if self.parse.adapter and self.parse.model:
            pairs.add((self.parse.adapter, self.parse.model))
        return pairs

    def price_of(self, adapter: str, model: str) -> ModelPrice | None:
        return self.prices.get(adapter, {}).get(model)

    def rate_limit_of(self, adapter: str, model: str) -> RateLimitProfile | None:
        return self.rate_limits.get(adapter, {}).get(model)


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
