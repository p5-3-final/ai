"""Adapter 공통 인터페이스."""

from dataclasses import dataclass
from typing import Protocol

from vlm_common.errors import VlmError

from ai_gateway.prompts import PromptMessage


@dataclass(frozen=True, slots=True)
class ImageInput:
    """모델에 보낼 이미지 한 장(블러 사본)."""

    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class Completion:
    """모델 응답 원문과 공급자가 알려준 토큰 수."""

    text: str
    input_tokens: int | None
    output_tokens: int | None


class ProviderUnavailableError(VlmError):
    """시간 초과, 연결 불가, 429·5xx. 호출자가 다시 시도할 수 있다(SPEC-009 R-18)."""

    code = "AI_PROVIDER_UNAVAILABLE"


class ProviderFailedError(VlmError):
    """공급자가 요청 자체를 거부했다(429 외 4xx). 다시 보내도 같을 가능성이 높다(R-19)."""

    code = "AI_PROVIDER_FAILED"


class VlmAdapter(Protocol):
    """VLM 공급자 하나. 새 공급자는 이 Protocol의 구현과 프로필만 추가한다(NFR-2)."""

    async def complete(
        self,
        *,
        model: str,
        message: PromptMessage,
        images: list[ImageInput],
        max_output_tokens: int,
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Completion:
        """모델을 한 번 부른다.

        Raises:
            ProviderUnavailableError: 일시적으로 쓸 수 없을 때.
            ProviderFailedError: 공급자가 요청을 거부했을 때.
        """
        ...
