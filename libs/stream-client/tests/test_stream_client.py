"""stream-service 내부 구역 읽기 클라이언트(SPEC-027 R-33).

stream-service는 MockTransport로 대신한다.
"""

from uuid import UUID

import httpx
import pytest
from vlm_common.http_call import CallRetry, Deadline
from vlm_stream_client.client import StreamApiClient, StreamApiError

SESSION = UUID("0192fb00-0000-7000-8000-000000000010")
ZONE = {
    "zoneId": "0192f100-0000-7000-8000-0000000000f1",
    "name": "A동 크레인 하부",
    "kind": "hazard",
    "polygon": [[0.1, 0.2], [0.6, 0.2], [0.6, 0.9]],
}
T0 = 1_762_000_000.0


async def _no_sleep(_s: float) -> None:
    return None


def _client(transport: httpx.MockTransport) -> StreamApiClient:
    return StreamApiClient(
        "http://stream-service:8080",
        caller="aggregator",
        timeout_s=5.0,
        retry=CallRetry(max_retries=2, base_delay_s=0.0),
        transport=transport,
        sleep=_no_sleep,
        clock=lambda: T0,
    )


async def test_spec027_r33_reads_session_zones_with_caller_and_deadline() -> None:
    """[SPEC-027 R-33] 내부 경로, X-Caller, X-Request-Deadline을 싣는다."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"zones": [ZONE], "updatedAt": None})

    deadline = Deadline.start(30, lambda: T0)
    async with _client(httpx.MockTransport(handler)) as client:
        zones = await client.get_session_zones(SESSION, deadline=deadline)

    [request] = seen
    assert request.url.path == f"/internal/v1/stream/sessions/{SESSION}/zones"
    assert request.headers["X-Caller"] == "aggregator"
    assert request.headers["X-Request-Deadline"] == str(deadline.at_ms)
    assert [z.kind for z in zones.zones] == ["hazard"]


async def test_spec027_r33_503_is_retried_then_404_is_not() -> None:
    """[SPEC-027 R-33] 읽기 전용 규칙: 503은 다시 부르고 404는 코드와 함께 끝난다."""
    replies = [
        httpx.Response(503, json={"type": "about:blank", "title": "x", "status": 503, "code": "S"}),
        httpx.Response(
            404, json={"type": "about:blank", "title": "x", "status": 404, "code": "STREAM_NF"}
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(StreamApiError) as raised:
            await client.get_session_zones(SESSION, deadline=Deadline.start(30, lambda: T0))

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (
        "STREAM_NF",
        404,
        False,
    )
