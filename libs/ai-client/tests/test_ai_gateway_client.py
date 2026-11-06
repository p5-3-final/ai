"""ai-gateway 클라이언트(SPEC-020 R-1·R-15, SPEC-021 R-8~R-13).

ai-gateway는 MockTransport로 대신한다.
"""

import json
from collections.abc import Awaitable, Callable
from uuid import UUID

import httpx
import pytest
from vlm_ai_client.client import AiGatewayClient, AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, EmbedRequest, ImageRef, ParseRequest
from vlm_common.http_call import CallRetry

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
CALL = UUID("0192f0e1-0000-7000-8000-000000000001")
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
OUTPUT = {"eventType": "fall", "verificationStatus": "confirmed"}
# 가짜 시계의 시작 시각(epoch 초)과 클라이언트 제한 시간
T0 = 1_762_000_000.0
TIMEOUT_S = 5.0

type Call = Callable[[AiGatewayClient], Awaitable[object]]


def _analyze_request() -> AnalyzeRequest:
    return AnalyzeRequest(
        task_type="event_judge",
        source_id=SOURCE,
        images=[ImageRef(storage_key=f"sources/{SOURCE}/x/blurred/{i}.jpg") for i in range(2)],
        variables={"eventType": "fall", "startSec": 0.0, "endSec": 10.0},
    )


def _analyze_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "callId": str(CALL),
            "taskType": "event_judge",
            "modelId": "gpt-6.1-terra",
            "promptVersion": "event_judge@3f2a9c1d7b4e",
            "taxonomyVersion": "v2",
            "output": OUTPUT,
        },
    )


def _embed_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "callId": str(CALL),
            "taskType": "embed_text",
            "modelId": "openai:text-embedding-3-small",
            "dimension": 2,
            "vectors": [[0.6, 0.8]],
        },
    )


def _parse_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "callId": str(CALL),
            "route": "video_retrieval",
            "targets": ["forklift"],
            "eventTypes": [],
            "visualQuery": "a photo of forklift",
            "parseMethod": "english-phrase",
            "rationale": "english phrase",
            "dictionaryVersion": "v1@4d1c0a9e7b2f",
            "promptVersion": None,
            "taxonomyVersion": "v2",
            "canonicalQuery": "v1:video_retrieval|forklift||english-phrase|v1@4d1c0a9e7b2f|v2",
        },
    )


def _problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "about:blank", "title": "x", "status": status, "code": code},
        headers={"Content-Type": "application/problem+json"},
    )


async def _analyze(client: AiGatewayClient) -> object:
    return await client.analyze(_analyze_request())


async def _embed(client: AiGatewayClient) -> object:
    return await client.embed(EmbedRequest(task_type="embed_text", texts=["지게차"]))


async def _parse(client: AiGatewayClient) -> object:
    return await client.parse(ParseRequest(query="forklift"))


_OK = {"analyze": _analyze_ok, "embed": _embed_ok, "parse": _parse_ok}
_CALLS: dict[str, Call] = {"analyze": _analyze, "embed": _embed, "parse": _parse}


class _Clock:
    """재시도 대기만큼만 흐르는 가짜 시계."""

    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


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
    gateway: _Gateway,
    clock: _Clock | None = None,
    *,
    retry: CallRetry | None = None,
    timeout_s: float = TIMEOUT_S,
) -> AiGatewayClient:
    clock = clock or _Clock()
    return AiGatewayClient(
        "http://ai-gateway:8000",
        caller="event-analyzer",
        timeout_s=timeout_s,
        retry=retry or CallRetry(max_retries=2, base_delay_s=0.5),
        transport=httpx.MockTransport(gateway),
        sleep=clock.sleep,
        clock=clock,
    )


async def test_spec020_r1_posts_analyze_with_caller_and_trace() -> None:
    gateway = _Gateway(_analyze_ok())

    async with _client(gateway) as client:
        response = await client.analyze(_analyze_request(), traceparent=TRACEPARENT)

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
    gateway = _Gateway(_analyze_ok())

    async with _client(gateway) as client:
        await client.analyze(_analyze_request())

    assert "traceparent" not in gateway.requests[0].headers


async def test_spec021_r13_embed_posts_body_and_parses_response() -> None:
    """[SPEC-021 R-13] `/v1/embed`를 계약 본문으로 부르고 생성 응답 모델로 돌려준다."""
    gateway = _Gateway(_embed_ok())

    async with _client(gateway) as client:
        response = await client.embed(
            EmbedRequest(task_type="embed_text", texts=["지게차"]), traceparent=TRACEPARENT
        )

    [sent] = gateway.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/embed")
    assert sent.headers["X-Caller"] == "event-analyzer"
    assert sent.headers["traceparent"] == TRACEPARENT
    # 넣지 않은 선택 필드(images)는 싣지 않는다.
    assert json.loads(sent.content) == {"taskType": "embed_text", "texts": ["지게차"]}
    assert (response.call_id, response.dimension, response.vectors) == (CALL, 2, [[0.6, 0.8]])


async def test_spec021_r13_parse_posts_body_and_parses_response() -> None:
    """[SPEC-021 R-13] `/v1/parse`를 계약 본문으로 부르고 생성 응답 모델로 돌려준다."""
    gateway = _Gateway(_parse_ok())

    async with _client(gateway) as client:
        response = await client.parse(ParseRequest(query="forklift"))

    [sent] = gateway.requests
    assert (sent.method, sent.url.path) == ("POST", "/v1/parse")
    assert json.loads(sent.content) == {"query": "forklift"}
    assert (response.route, response.targets, response.parse_method) == (
        "video_retrieval",
        ["forklift"],
        "english-phrase",
    )


@pytest.mark.parametrize("kind", ["analyze", "embed", "parse"])
async def test_spec021_r8_every_call_carries_one_deadline(kind: str) -> None:
    """[SPEC-021 R-8] 마감 = 첫 시도 시각 + 제한 시간. 재시도도 같은 값이다.

    시도별 제한 시간은 min(제한 시간, 남은 시간)이다.
    """
    gateway = _Gateway(httpx.ConnectError("refused"), _OK[kind]())
    clock = _Clock()

    async with _client(gateway, clock) as client:
        await _CALLS[kind](client)

    first, second = gateway.requests
    expected = str(int((T0 + TIMEOUT_S) * 1000))
    assert first.headers["X-Request-Deadline"] == expected
    assert second.headers["X-Request-Deadline"] == expected
    assert first.extensions["timeout"]["read"] == pytest.approx(TIMEOUT_S)
    # 재시도 전 0.5초를 기다렸으므로 남은 4.5초가 둘째 시도의 제한 시간이다.
    assert second.extensions["timeout"]["read"] == pytest.approx(TIMEOUT_S - 0.5)


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.ConnectTimeout("syn"), httpx.PoolTimeout("pool")],
)
async def test_spec021_r9_analyze_retries_only_when_not_connected(failure: Exception) -> None:
    """[SPEC-021 R-9] 요청이 닿지 않은 연결 실패만 지수 대기 후 다시 부른다."""
    gateway = _Gateway(failure, failure, _analyze_ok())
    clock = _Clock()

    async with _client(gateway, clock) as client:
        response = await client.analyze(_analyze_request())

    assert response.call_id == CALL
    assert len(gateway.requests) == 3
    assert clock.sleeps == [0.5, 1.0]


@pytest.mark.parametrize(
    "failure",
    [
        _problem(502, "AI_PROVIDER_FAILED"),
        _problem(503, "AI_PROVIDER_UNAVAILABLE"),
        _problem(504, "AI_DEADLINE_EXCEEDED"),
        httpx.ReadTimeout("slow"),
        httpx.WriteTimeout("slow"),
        httpx.ReadError("reset"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("closed"),
    ],
)
async def test_spec021_r9_analyze_does_not_retry_after_sending(
    failure: httpx.Response | Exception,
) -> None:
    """[SPEC-021 R-9] 보냈을 수 있는 실패(502·503·504·시간 초과·보낸 뒤 끊김)는 한 번만 부른다."""
    gateway = _Gateway(failure, _analyze_ok())
    clock = _Clock()

    async with _client(gateway, clock) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_analyze_request())

    assert len(gateway.requests) == 1
    assert clock.sleeps == []
    assert raised.value.retryable is False


async def test_spec021_r9_analyze_gives_up_after_configured_retries() -> None:
    """[SPEC-021 R-9] 연결 실패가 이어지면 설정 횟수만큼만 다시 부르고 끝낸다."""
    failure = httpx.ConnectError("refused")
    gateway = _Gateway(failure, failure, failure)
    clock = _Clock()

    async with _client(gateway, clock) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_analyze_request())

    assert (raised.value.code, raised.value.retryable) == ("AI_CONNECTION_FAILED", True)
    assert len(gateway.requests) == 3
    assert clock.sleeps == [0.5, 1.0]


async def test_spec021_r9_zero_retries_calls_once() -> None:
    gateway = _Gateway(httpx.ConnectError("refused"))

    async with _client(gateway, retry=CallRetry(max_retries=0, base_delay_s=1)) as client:
        with pytest.raises(AiGatewayError):
            await client.analyze(_analyze_request())

    assert len(gateway.requests) == 1


@pytest.mark.parametrize("kind", ["embed", "parse"])
@pytest.mark.parametrize(
    "failure",
    [
        _problem(503, "AI_PROVIDER_UNAVAILABLE"),
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("syn"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("closed"),
    ],
)
async def test_spec021_r10_read_calls_retry_503_and_connection_failures(
    kind: str, failure: httpx.Response | Exception
) -> None:
    """[SPEC-021 R-10] 읽기 전용 호출은 503과 연결 실패에 다시 부른다."""
    gateway = _Gateway(failure, _OK[kind]())
    clock = _Clock()

    async with _client(gateway, clock) as client:
        await _CALLS[kind](client)

    assert len(gateway.requests) == 2
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize("kind", ["embed", "parse"])
@pytest.mark.parametrize(
    "failure",
    [
        _problem(502, "AI_PROVIDER_FAILED"),
        _problem(504, "AI_DEADLINE_EXCEEDED"),
        _problem(400, "AI_INVALID_REQUEST"),
        _problem(429, "AI_RATE_LIMITED"),
        httpx.ReadTimeout("slow"),
    ],
)
async def test_spec021_r10_read_calls_do_not_retry_other_failures(
    kind: str, failure: httpx.Response | Exception
) -> None:
    """[SPEC-021 R-10] 502·504·4xx·응답 시간 초과는 다시 부르지 않는다."""
    gateway = _Gateway(failure, _OK[kind]())

    async with _client(gateway) as client:
        with pytest.raises(AiGatewayError) as raised:
            await _CALLS[kind](client)

    assert len(gateway.requests) == 1
    assert raised.value.retryable is False


@pytest.mark.parametrize("kind", ["analyze", "embed"])
async def test_spec021_r11_no_retry_when_wait_would_pass_deadline(kind: str) -> None:
    """[SPEC-021 R-11] 다음 대기가 마감을 넘기면 기다리지 않고 마지막 오류로 끝낸다."""
    failure = httpx.ConnectError("refused")
    gateway = _Gateway(failure, failure, failure)
    clock = _Clock()

    # 제한 1초: 0.5초 대기 뒤 둘째 실패, 다음 1초 대기는 마감(1초)을 넘긴다.
    async with _client(gateway, clock, timeout_s=1.0) as client:
        with pytest.raises(AiGatewayError) as raised:
            await _CALLS[kind](client)

    assert len(gateway.requests) == 2
    assert clock.sleeps == [0.5]
    assert raised.value.code == "AI_CONNECTION_FAILED"


@pytest.mark.parametrize(
    ("failure", "code", "status"),
    [
        (_problem(504, "AI_DEADLINE_EXCEEDED"), "AI_DEADLINE_EXCEEDED", 504),
        (_problem(403, "AI_CALLER_FORBIDDEN"), "AI_CALLER_FORBIDDEN", 403),
        (httpx.Response(418, text="teapot"), "AI_HTTP_418", 418),
        (httpx.Response(500, json={"title": "no code"}), "AI_HTTP_500", 500),
        (httpx.ReadTimeout("slow"), "AI_TIMEOUT", None),
        (httpx.ReadError("reset"), "AI_CONNECTION_FAILED", None),
        (httpx.Response(200, json={"callId": "not-a-uuid"}), "AI_INVALID_RESPONSE", 200),
    ],
)
async def test_spec021_r12_failure_has_one_error_code(
    failure: httpx.Response | Exception, code: str, status: int | None
) -> None:
    """[SPEC-021 R-12] 실패는 ProblemDetail 코드, 없으면 AI_HTTP_<상태>, 그 밖은 정해진 코드다."""
    gateway = _Gateway(failure)

    async with _client(gateway) as client:
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(_analyze_request())

    assert (raised.value.code, raised.value.status) == (code, status)
