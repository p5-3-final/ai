import base64
import json
from collections.abc import Callable

import httpx2
import pytest
from ai_gateway.adapters.base import (
    Completion,
    Embeddings,
    ImageInput,
    ProviderFailedError,
    ProviderUnavailableError,
)
from ai_gateway.adapters.openai import OpenAiAdapter
from ai_gateway.prompts import PromptMessage

MESSAGE = PromptMessage(
    task_type="event_judge", fixed_prefix="# 공통 규칙\n", variable_part="# 작업\n입력 JSON"
)
IMAGES = [ImageInput(b"jpeg-0", "image/jpeg"), ImageInput(b"png-1", "image/png")]
TRACEPARENT = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
Handler = Callable[[httpx2.Request], httpx2.Response]


def _completion(text: str | None) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {"prompt_tokens": 1500, "completion_tokens": 120, "total_tokens": 1620},
    }


def _adapter(handler: Handler, *, compatible: bool = False) -> OpenAiAdapter:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return OpenAiAdapter(
        api_key="sk-test",
        base_url="http://ollama:11434/v1" if compatible else None,
        compatible=compatible,
        http_client=client,
    )


async def _complete(adapter: OpenAiAdapter, retries: int = 0) -> Completion:
    return await adapter.complete(
        model="m",
        message=MESSAGE,
        images=IMAGES,
        max_output_tokens=800,
        timeout_s=5,
        max_retries=retries,
        traceparent=None,
    )


async def test_spec009_r1_r12_sends_prefix_as_system_and_images_in_order() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion('{"a": 1}'))

    completion = await _complete(_adapter(handler))

    assert completion.text == '{"a": 1}'
    assert (completion.input_tokens, completion.output_tokens) == (1500, 120)
    [request] = seen
    body = json.loads(request.content)
    assert body["model"] == "m"
    assert body["max_completion_tokens"] == 800
    system, user = body["messages"]
    # 고정 접두부는 system으로 따로 보내 공급자 프롬프트 캐시를 받는다(R-12).
    assert system == {"role": "system", "content": MESSAGE.fixed_prefix}
    assert user["role"] == "user"
    text, first, second = user["content"]
    assert text == {"type": "text", "text": MESSAGE.variable_part}
    encoded = base64.b64encode(b"jpeg-0").decode()
    assert first["image_url"]["url"] == f"data:image/jpeg;base64,{encoded}"
    assert second["image_url"]["url"].startswith("data:image/png;base64,")


async def test_spec009_r1_compatible_server_gets_max_tokens() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion("{}"))

    await _complete(_adapter(handler, compatible=True))

    [request] = seen
    assert str(request.url) == "http://ollama:11434/v1/chat/completions"
    body = json.loads(request.content)
    # Ollama 같은 호환 서버는 max_completion_tokens를 모를 수 있어 예전 이름을 쓴다.
    assert body["max_tokens"] == 800
    assert "max_completion_tokens" not in body


def _status(code: int) -> Handler:
    return lambda _request: httpx2.Response(code, json={"error": {"message": "x"}})


def _raise(error: Exception) -> Handler:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        raise error

    return handler


@pytest.mark.parametrize(
    "handler",
    [
        _status(429),
        _status(500),
        _status(503),
        _raise(httpx2.ReadTimeout("slow")),
        _raise(httpx2.ConnectError("down")),
    ],
)
async def test_spec009_r18_transient_errors_are_unavailable(handler: Handler) -> None:
    with pytest.raises(ProviderUnavailableError):
        await _complete(_adapter(handler))


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
async def test_spec009_r19_rejected_requests_are_failed(code: int) -> None:
    with pytest.raises(ProviderFailedError):
        await _complete(_adapter(_status(code)))


async def test_spec009_r18_retries_transient_error_up_to_profile_limit() -> None:
    replies = [httpx2.Response(503), httpx2.Response(200, json=_completion("{}"))]

    completion = await _complete(_adapter(lambda _r: replies.pop(0)), retries=1)

    assert completion.text == "{}"
    assert replies == []


async def test_spec009_r1_missing_content_is_empty_text() -> None:
    reply = _completion(None)

    completion = await _complete(_adapter(lambda _r: httpx2.Response(200, json=reply)))

    assert completion.text == ""


async def test_spec009_nfr5_traceparent_is_forwarded_to_provider() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion("{}"))

    await _adapter(handler).complete(
        model="m",
        message=MESSAGE,
        images=[],
        max_output_tokens=10,
        timeout_s=5,
        max_retries=0,
        traceparent=TRACEPARENT,
    )

    assert seen[0].headers["traceparent"] == TRACEPARENT


def _embeddings(vectors: list[list[float]], order: list[int] | None = None) -> dict[str, object]:
    indexes = order if order is not None else list(range(len(vectors)))
    return {
        "object": "list",
        "model": "m",
        "data": [{"object": "embedding", "index": i, "embedding": vectors[i]} for i in indexes],
        "usage": {"prompt_tokens": 9, "total_tokens": 9},
    }


async def _embed(adapter: OpenAiAdapter, texts: list[str], retries: int = 0) -> Embeddings:
    return await adapter.embed(
        model="bge-m3", texts=texts, timeout_s=5, max_retries=retries, traceparent=TRACEPARENT
    )


async def test_spec011_r3_embeds_texts_in_input_order() -> None:
    """[SPEC-011 R-3] 공급자가 순서를 바꿔 돌려줘도 index로 입력 순서를 맞춘다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_embeddings([[1.0, 0.0], [0.0, 1.0]], order=[1, 0]))

    result = await _embed(_adapter(handler, compatible=True), ["첫째", "둘째"])

    assert result.vectors == [[1.0, 0.0], [0.0, 1.0]]
    assert result.input_tokens == 9
    [request] = seen
    assert request.url.path == "/v1/embeddings"
    body = json.loads(request.content)
    assert (body["model"], body["input"]) == ("bge-m3", ["첫째", "둘째"])
    assert request.headers["traceparent"] == TRACEPARENT


@pytest.mark.parametrize(
    "handler",
    [
        _status(429),
        _status(500),
        _raise(httpx2.ReadTimeout("slow")),
        _raise(httpx2.ConnectError("down")),
    ],
)
async def test_spec011_r20_transient_embedding_errors_are_unavailable(handler: Handler) -> None:
    """[SPEC-011 R-20] 시간 초과·연결 불가·429·5xx는 일시적으로 사용할 수 없음이다."""
    with pytest.raises(ProviderUnavailableError):
        await _embed(_adapter(handler), ["a"])


@pytest.mark.parametrize("code", [400, 401, 404])
async def test_spec011_r21_rejected_embedding_requests_are_failed(code: int) -> None:
    """[SPEC-011 R-21] 429 외 4xx는 공급자 거부다."""
    with pytest.raises(ProviderFailedError):
        await _embed(_adapter(_status(code)), ["a"])


@pytest.mark.parametrize("compatible", [False, True])
async def test_spec012_r10_sends_temperature_when_given(compatible: bool) -> None:
    """[SPEC-012 R-10] query_parse는 temperature 0으로 부른다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion("{}"))

    await _adapter(handler, compatible=compatible).complete(
        model="m",
        message=MESSAGE,
        images=[],
        max_output_tokens=200,
        timeout_s=5,
        max_retries=0,
        traceparent=None,
        temperature=0.0,
    )

    [request] = seen
    assert json.loads(request.content)["temperature"] == 0


async def test_spec012_r10_omits_temperature_by_default() -> None:
    """[SPEC-012 R-10] /v1/analyze는 지금처럼 공급자 기본값을 쓴다."""
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_completion("{}"))

    await _complete(_adapter(handler))

    [request] = seen
    assert "temperature" not in json.loads(request.content)


async def test_spec016_r13_connection_failure_was_not_sent() -> None:
    """[SPEC-016 R-13] 연결조차 못 한 요청은 보내지 않은 것으로 본다."""
    with pytest.raises(ProviderUnavailableError) as caught:
        await _complete(_adapter(_raise(httpx2.ConnectError("down"))))

    assert caught.value.sent is False


@pytest.mark.parametrize("handler", [_raise(httpx2.ReadTimeout("slow")), _status(503)])
async def test_spec016_r22_timeout_and_server_errors_may_have_been_sent(handler: Handler) -> None:
    """[SPEC-016 R-22] 시간 초과·5xx는 공급자가 처리했을 수 있다."""
    with pytest.raises(ProviderUnavailableError) as caught:
        await _complete(_adapter(handler))

    assert caught.value.sent is True
