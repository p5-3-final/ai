"""analysis-api 내부 읽기 클라이언트(SPEC-027 R-3, R-22, R-23).

analysis-api는 MockTransport로 대신한다.
"""

from collections.abc import Callable
from uuid import UUID

import httpx
import pytest
from vlm_analysis_client.client import AnalysisApiClient, AnalysisApiError
from vlm_common.http_call import CallRetry, Deadline

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
JOB = UUID("0192f0d5-0000-7000-8000-000000000001")
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
T0 = 1_762_000_000.0
EVENT = {
    "eventId": "0192f300-0000-5000-8000-000000000101",
    "chunkId": "0192f0d6-0000-7000-8000-000000000001",
    "eventType": "ppe_missing",
    "startMs": 95000,
    "endMs": 105000,
    "trackId": None,
    "zoneId": None,
    "zoneName": None,
    "judgmentMethod": "rule+vlm",
    "score": 0.82,
    "reviewRequired": False,
    "analysis": {"eventType": "ppe_missing"},
}

type Handler = Callable[[httpx.Request], httpx.Response]


class _Clock:
    def __init__(self) -> None:
        self.now = T0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _client(handler: Handler, clock: _Clock, retries: int = 2) -> AnalysisApiClient:
    return AnalysisApiClient(
        "http://analysis-api:8085",
        caller="aggregator",
        timeout_s=10.0,
        retry=CallRetry(max_retries=retries, base_delay_s=1.0),
        transport=httpx.MockTransport(handler),
        sleep=clock.sleep,
        clock=clock,
    )


def _page(items: list[dict[str, object]], cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "nextCursor": cursor})


def _problem(status: int, code: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": "about:blank", "title": "x", "status": status, "code": code},
        headers={"content-type": "application/problem+json"},
    )


async def test_spec027_r3_list_events_sends_path_headers_and_paging() -> None:
    """[SPEC-027 R-3] 경로·X-Caller·X-Request-Deadline·traceparent·cursor·limit을 싣는다."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _page([EVENT], "next-1")

    clock = _Clock()
    deadline = Deadline.start(300, clock)
    async with _client(handler, clock) as client:
        page = await client.list_job_events(
            SOURCE, JOB, deadline=deadline, cursor="c0", limit=1000, traceparent=TRACEPARENT
        )

    [request] = seen
    assert request.url.path == f"/internal/v1/sources/{SOURCE}/jobs/{JOB}/events"
    assert dict(request.url.params) == {"cursor": "c0", "limit": "1000"}
    assert request.headers["X-Caller"] == "aggregator"
    assert request.headers["X-Request-Deadline"] == str(deadline.at_ms)
    assert request.headers["traceparent"] == TRACEPARENT
    assert page.next_cursor == "next-1"
    assert page.items[0].event_id == UUID(str(EVENT["eventId"]))


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("list_job_tracks", "tracks", {"items": [], "nextCursor": None}),
        ("list_job_zone_presences", "zone-presences", {"items": [], "nextCursor": None}),
    ],
)
async def test_spec027_r3_job_lists_use_their_paths(
    method: str, path: str, body: dict[str, object]
) -> None:
    """[SPEC-027 R-3] 트랙·구역 머문 구간도 같은 쪽 나누기 규칙이다. 첫 쪽은 cursor가 없다."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    clock = _Clock()
    async with _client(handler, clock) as client:
        page = await getattr(client, method)(SOURCE, JOB, deadline=Deadline.start(300, clock))

    assert seen[0].url.path == f"/internal/v1/sources/{SOURCE}/jobs/{JOB}/{path}"
    assert "cursor" not in seen[0].url.params
    assert page.next_cursor is None


async def test_spec027_r3_zones_and_final_state() -> None:
    """[SPEC-027 R-3·R-5] 구역 목록과 마지막 상태는 영상 경로다."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/zones"):
            return httpx.Response(
                200,
                json={"items": [{"zoneId": str(JOB), "name": "A", "kind": "hazard"}]},
            )
        return httpx.Response(
            200,
            json={"sourceId": str(SOURCE), "jobId": str(JOB), "zoneStates": [], "activeRisks": []},
        )

    clock = _Clock()
    async with _client(handler, clock) as client:
        zones = await client.list_source_zones(SOURCE, deadline=Deadline.start(300, clock))
        final = await client.get_source_final_state(SOURCE, deadline=Deadline.start(300, clock))

    assert [z.kind for z in zones.items] == ["hazard"]
    assert final.job_id == JOB


async def test_spec027_r22_retries_503_and_connection_failures() -> None:
    """[SPEC-027 R-22] 503·연결 실패는 2회 지수 대기로 다시 부른다(읽기 전용 규칙)."""
    replies: list[httpx.Response | Exception] = [
        _problem(503, "ANALYSIS_UNAVAILABLE"),
        httpx.ConnectError("refused"),
        _page([]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    clock = _Clock()
    async with _client(handler, clock) as client:
        page = await client.list_job_events(SOURCE, JOB, deadline=Deadline.start(300, clock))

    assert page.items == []
    assert clock.slept == [1.0, 2.0]


async def test_spec027_r22_exhausted_retries_raise_last_retryable_error() -> None:
    """[SPEC-027 R-22] 재시도를 다 쓰면 마지막 503 오류를 던진다."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _problem(503, "ANALYSIS_UNAVAILABLE")

    clock = _Clock()
    async with _client(handler, clock) as client:
        with pytest.raises(AnalysisApiError) as raised:
            await client.list_job_events(SOURCE, JOB, deadline=Deadline.start(300, clock))

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (
        "ANALYSIS_UNAVAILABLE",
        503,
        True,
    )
    assert calls == 3


async def test_spec027_r22_stops_when_next_wait_reaches_deadline() -> None:
    """[SPEC-027 R-22] 다음 대기가 마감을 넘기면 멈춘다(SPEC-021 R-11)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _problem(503, "ANALYSIS_UNAVAILABLE")

    clock = _Clock()
    async with _client(handler, clock) as client:
        with pytest.raises(AnalysisApiError):
            await client.list_job_events(SOURCE, JOB, deadline=Deadline.start(0.5, clock))

    assert clock.slept == []


@pytest.mark.parametrize(
    ("reply", "code", "status"),
    [
        (_problem(404, "ANALYSIS_JOB_NOT_FOUND"), "ANALYSIS_JOB_NOT_FOUND", 404),
        (_problem(400, "ANALYSIS_INVALID_REQUEST"), "ANALYSIS_INVALID_REQUEST", 400),
        (_problem(504, "ANALYSIS_DEADLINE_EXCEEDED"), "ANALYSIS_DEADLINE_EXCEEDED", 504),
        (httpx.Response(500, text="boom"), "ANALYSIS_HTTP_500", 500),
        (httpx.Response(200, json={"items": "x"}), "ANALYSIS_INVALID_RESPONSE", 200),
    ],
)
async def test_spec027_r23_error_codes(reply: httpx.Response, code: str, status: int) -> None:
    """[SPEC-027 R-23] ProblemDetail code, 없으면 상태, 계약과 맞지 않는 응답은 따로 표시한다.

    503 말고는 다시 부르지 않는다.
    """
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return reply

    clock = _Clock()
    async with _client(handler, clock) as client:
        with pytest.raises(AnalysisApiError) as raised:
            await client.list_job_events(SOURCE, JOB, deadline=Deadline.start(300, clock))

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (code, status, False)
    assert calls == 1


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ReadTimeout("slow"), "ANALYSIS_TIMEOUT"),
        (httpx.ConnectError("refused"), "ANALYSIS_CONNECTION_FAILED"),
    ],
)
async def test_spec027_r22_transport_failures_have_codes(error: Exception, code: str) -> None:
    """[SPEC-027 R-22] 시간 초과·연결 실패도 코드가 있다. 응답 시간 초과는 다시 부르지 않는다."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    clock = _Clock()
    async with _client(handler, clock, retries=0) as client:
        with pytest.raises(AnalysisApiError) as raised:
            await client.list_job_events(SOURCE, JOB, deadline=Deadline.start(300, clock))

    assert (raised.value.code, raised.value.status) == (code, None)
