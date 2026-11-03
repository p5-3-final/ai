"""OpenAI Chat Completions Adapter. `openai`와 `openai_compatible`(Ollama 등)이 함께 쓴다.

공급자 SDK(openai)는 이 파일에서만 import한다(SPEC-009 NFR-2).
"""

import base64

import httpx2
import openai
from openai import AsyncOpenAI
from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from ai_gateway.adapters.base import (
    Completion,
    Embeddings,
    ImageInput,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ai_gateway.prompts import PromptMessage

# 호환 서버는 키를 확인하지 않지만 SDK가 빈 키를 거부한다.
_UNUSED_KEY = "unused"


class OpenAiAdapter:
    """Chat Completions API로 VLM을 부른다.

    Args:
        api_key: 공급자 API 키. 호환 서버면 None이어도 된다.
        base_url: 호환 서버 주소. None이면 OpenAI 기본 주소다.
        compatible: 호환 서버 여부. 출력 상한 필드 이름이 다르다.
        http_client: 테스트에서 HTTP를 바꿔 넣을 때 쓴다.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        compatible: bool,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key or _UNUSED_KEY, base_url=base_url, http_client=http_client
        )
        self._compatible = compatible

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
        # 재시도(지수 백오프)는 SDK가 429·5xx·연결 오류에만 한다. 횟수는 프로필 값이다.
        client = self._client.with_options(timeout=timeout_s, max_retries=max_retries)
        content: list[ChatCompletionContentPartTextParam | ChatCompletionContentPartImageParam] = [
            {"type": "text", "text": message.variable_part}
        ]
        content.extend(_image_part(i) for i in images)
        messages: list[ChatCompletionMessageParam] = [
            # 고정 접두부를 system으로 따로 보내야 공급자의 프롬프트 캐시가 앞부분을 재사용한다.
            {"role": "system", "content": message.fixed_prefix},
            {"role": "user", "content": content},
        ]
        headers = {"traceparent": traceparent} if traceparent else None
        try:
            if self._compatible:
                # Ollama 등 호환 서버는 max_completion_tokens를 모를 수 있어 예전 이름을 쓴다.
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_tokens=max_output_tokens,
                    extra_headers=headers,
                )
            else:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    max_completion_tokens=max_output_tokens,
                    extra_headers=headers,
                )
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            raise _provider_error(e) from e
        usage = response.usage
        text = response.choices[0].message.content if response.choices else None
        return Completion(
            text=text or "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
        )

    async def embed(
        self,
        *,
        model: str,
        texts: list[str],
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Embeddings:
        client = self._client.with_options(timeout=timeout_s, max_retries=max_retries)
        headers = {"traceparent": traceparent} if traceparent else None
        try:
            response = await client.embeddings.create(
                model=model, input=texts, extra_headers=headers
            )
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            raise _provider_error(e) from e
        # 응답 순서는 보장되지 않아 index로 입력 순서를 맞춘다(SPEC-011 R-3).
        data = sorted(response.data, key=lambda d: d.index)
        return Embeddings(
            vectors=[d.embedding for d in data],
            input_tokens=response.usage.prompt_tokens if response.usage else None,
        )


def _provider_error(error: openai.APIError) -> ProviderUnavailableError | ProviderFailedError:
    """공급자 오류를 다시 시도할 수 있는지로 나눈다(SPEC-009 R-18·R-19, SPEC-011 R-20·R-21)."""
    if isinstance(error, openai.APIStatusError):
        if error.status_code == 429 or error.status_code >= 500:
            return ProviderUnavailableError(f"provider returned {error.status_code}")
        return ProviderFailedError(f"provider rejected the request: {error.status_code}")
    # 시간 초과(APITimeoutError)도 연결 오류에 속한다.
    return ProviderUnavailableError(f"provider is unreachable: {type(error).__name__}")


def _image_part(image: ImageInput) -> ChatCompletionContentPartImageParam:
    encoded = base64.b64encode(image.data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{image.media_type};base64,{encoded}"}}
