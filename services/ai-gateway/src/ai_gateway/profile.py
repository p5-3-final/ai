"""ai-gateway 프로필(Git으로 관리하는 TOML).

모델·공급자·한도를 코드가 아니라 프로필로 바꾸게 한다(docs ADR-0008, SPEC-009 US-3).
"""

import re
from typing import Literal, Self

from pydantic import Field, HttpUrl, model_validator
from vlm_common.profile import Profile, ProfileError

AdapterKind = Literal["openai", "openai_compatible", "mock"]

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


class ImagesProfile(Profile):
    # 블러 사본 경로만 받는다(SPEC-009 R-9). 패턴은 키 전체와 맞아야 한다.
    allowed_patterns: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _compiles(self) -> Self:
        for pattern in self.allowed_patterns:
            try:
                re.compile(pattern.replace(SOURCE_ID_PLACEHOLDER, "x"))
            except re.error as e:
                raise ValueError(f"invalid image pattern {pattern!r}: {e}") from e
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


class GatewayProfile(Profile):
    callers: CallersProfile
    images: ImagesProfile
    adapters: dict[str, AdapterProfile] = Field(default_factory=dict)
    tasks: dict[str, TaskProfile]

    @model_validator(mode="after")
    def _adapter_refs(self) -> Self:
        for name, task in self.tasks.items():
            if task.adapter is not None and task.adapter not in self.adapters:
                raise ValueError(f"task {name} refers to unknown adapter {task.adapter!r}")
        return self


def check_profile(profile: GatewayProfile, *, allow_mock: bool, has_openai_key: bool) -> None:
    """환경과 맞춰 봐야 하는 규칙을 검사한다. 프로필 파일만으로는 알 수 없는 것들이다.

    Raises:
        ProfileError: mock 허용 없이 mock Adapter를 쓰거나(SPEC-009 R-15),
            API 키 없이 openai Adapter를 쓸 때.
    """
    used = {t.adapter for t in profile.tasks.values() if t.adapter is not None}
    for name in sorted(used):
        kind = profile.adapters[name].kind
        if kind == "mock" and not allow_mock:
            raise ProfileError(f"adapter {name} is mock but mock is not allowed")
        if kind == "openai" and not has_openai_key:
            raise ProfileError(f"adapter {name} is openai but no API key is set")
