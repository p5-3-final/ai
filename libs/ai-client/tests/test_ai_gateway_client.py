"""ai-gateway 클라이언트(SPEC-020 R-1, R-11, R-14, R-15). ai-gateway는 MockTransport로 대신한다."""

import json
from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from vlm_ai_client.client import (
    AiGatewayClient,
    AiGatewayError,
    AnalyzeImage,
    AnalyzeRequest,
    CallRetry,
)

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
CALL = UUID("0192f0e1-0000-7000-8000-000000000001")
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
OUTPUT = {"eventType": "fall", "verificationStatus": "confirmed"}

type Handler = Callable[[httpx.Request], httpx.Response]


def _request() -> AnalyzeRequest:
    return AnalyzeRequest(
        task_type="event_judge",
        source_id=SOURCE,
        images=[AnalyzeImage(storage_key=f"sources/{SOURCE}/x/blurred/{i}.jpg") for i in range(2)],
        variables={"eventType": "fall", "startSec": 0.0, "endSec": 10.0},
    )


def _ok(task_type: str = "event_judge") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "callId": str(CALL),
            "taskType": task_type,
            "modelId": "gpt-6.1-terra",
            "promptVersion": "event_judge@3f2a9c1d7b4e",
            "taxonomyVersion": "v2",
            "output": OUTPUT,
        },
    )


def _problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "about:blank", "title": "x", "status": status, "code": code},
        headers={"Content-Type": "application/problem+json"},
    )


class _Gateway:
    """정해 둔 응답을 차례로 돌려주는 가짜 ai-gateway. 받은 요청을 남긴다."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _client(
    gateway: _Gateway, retry: CallRetry | None = None, sleeps: list[float] | None = None
) -> AiGatewayClient:
    recorded = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        recorded.append(seconds)

    return AiGatewayClient(
        "http://ai-gateway:8000",
        caller="event-analyzer",
        timeout_s=5,
        retry=retry or CallRetry(max_retries=2, base_delay_s=0.5),
        transport=httpx.MockTransport(gateway),
        sleep=sleep,
    )


async def test_spec020_r1_posts_analyze_with_caller_and_trace() -> None:
    gateway = _Gateway(_ok())

    async with _client(gateway) as client:
        response = await client.analyze(_request(), traceparent=TRACEPARENT)

    [sent] = gateway.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/analyze")
    assert sent.headers["X-Caller"] == "event-analyzer"
    assert sent.headers["traceparent"] == TRACEPARENT
    assert json.loads(sent.content) == {
        "taskType": "event_judge",
        "sourceId": str(SOURCE),
        "images": [{"storageKey": f"sources/{SOURCE}/x/blurred/{i}.jpg"} for i in range(2)],
        "variables": {"eventType": "fall", "startSec": 0.0, "endSec": 10.0},
    }
    assert response.call_id == CALL
    assert response.model_id == "gpt-6.1-terra"
    assert response.prompt_version == "event_judge@3f2a9c1d7b4e"
    assert response.output == OUTPUT


async def test_spec020_r1_no_trace_header_without_traceparent() -> None:
    gateway = _Gateway(_ok())

    async with _client(gateway) as client:
        await client.analyze(_request())

    assert "traceparent" not in gateway.requests[0].headers


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, "AI_CALLER_FORBIDDEN"), (404, "AI_IMAGE_NOT_FOUND"), (400, "AI_UNKNOWN_TASK_TYPE")],
)
async def test_spec020_r15_client_errors_are_not_retried(status: int, code: str) -> None:
    gateway = _Gateway(_problem(status, code))

    async with _client(gateway) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_request())

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (code, status, False)
    assert len(gateway.requests) == 1


async def test_spec020_r15_error_without_problem_code_uses_status() -> None:
    gateway = _Gateway(httpx.Response(418, text="teapot"))

    async with _client(gateway) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_request())

    assert (raised.value.code, raised.value.status) == ("AI_HTTP_418", 418)


@pytest.mark.parametrize(
    "failure",
    [
        _problem(502, "AI_PROVIDER_FAILED"),
        _problem(503, "AI_PROVIDER_UNAVAILABLE"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
    ],
)
async def test_spec020_r11_transient_failure_is_retried(
    failure: httpx.Response | Exception,
) -> None:
    gateway = _Gateway(failure, _ok())
    sleeps: list[float] = []

    async with _client(gateway, sleeps=sleeps) as client:
        response = await client.analyze(_request())

    assert response.call_id == CALL
    assert len(gateway.requests) == 2
    assert sleeps == [0.5]


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (_problem(503, "AI_PROVIDER_UNAVAILABLE"), "AI_PROVIDER_UNAVAILABLE"),
        (httpx.ReadTimeout("slow"), "AI_TIMEOUT"),
        (httpx.ConnectError("refused"), "AI_CONNECTION_FAILED"),
    ],
)
async def test_spec020_r11_gives_up_after_configured_retries(
    failure: httpx.Response | Exception, code: str
) -> None:
    gateway = _Gateway(failure, failure, failure)
    sleeps: list[float] = []

    async with _client(gateway, sleeps=sleeps) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_request())

    assert (raised.value.code, raised.value.retryable) == (code, True)
    # 첫 시도 + 재시도 2번, 대기는 지수로 는다.
    assert len(gateway.requests) == 3
    assert sleeps == [0.5, 1.0]


async def test_spec020_r11_zero_retries_calls_once() -> None:
    gateway = _Gateway(_problem(502, "AI_PROVIDER_FAILED"))

    async with _client(gateway, retry=CallRetry(max_retries=0, base_delay_s=1)) as client:
        with pytest.raises(AiGatewayError):
            await client.analyze(_request())

    assert len(gateway.requests) == 1


async def test_spec020_r12_response_not_matching_contract_is_an_error() -> None:
    gateway = _Gateway(httpx.Response(200, json={"callId": "not-a-uuid"}))

    async with _client(gateway) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_request())

    assert (raised.value.code, raised.value.retryable) == ("AI_INVALID_RESPONSE", False)
