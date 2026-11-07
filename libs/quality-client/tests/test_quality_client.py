"""quality-service 성능 Gate 호출 클라이언트(SPEC-030 R-19).

quality-service는 MockTransport로 대신한다.
"""

import json
from uuid import UUID

import httpx
import pytest
from vlm_common.http_call import CallRetry, Deadline
from vlm_quality_client.client import QualityApiClient, QualityApiError
from vlm_quality_client.models import StageGateRequest, StageGateResult

BASELINE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
CANDIDATE = UUID("0192f0d2-0000-7000-8000-0000000000bb")
RESULT = {
    "passed": False,
    "maxRegressionRatio": 0.2,
    "stages": [
        {
            "stageId": "analyzer.judge",
            "baselineP95Ms": 3400.0,
            "candidateP95Ms": 4300.0,
            "ratio": 1.265,
            "baselineCount": 30,
            "candidateCount": 31,
            "status": "failed",
        },
        {
            "stageId": "normalize.transcode",
            "baselineP95Ms": 8000.0,
            "candidateP95Ms": None,
            "ratio": None,
            "baselineCount": 3,
            "candidateCount": 0,
            "status": "skipped",
        },
    ],
}
T0 = 1_762_000_000.0


def _problem(status: int, code: str) -> httpx.Response:
    body = {"type": "about:blank", "title": "x", "status": status, "code": code}
    return httpx.Response(status, json=body)


async def _no_sleep(_s: float) -> None:
    return None


def _client(transport: httpx.MockTransport) -> QualityApiClient:
    return QualityApiClient(
        "http://quality-service:8088",
        caller="quality-eval",
        timeout_s=5.0,
        retry=CallRetry(max_retries=2, base_delay_s=0.0),
        transport=transport,
        sleep=_no_sleep,
        clock=lambda: T0,
    )


def _request(**extra: object) -> StageGateRequest:
    return StageGateRequest.model_validate(
        {"baselineSourceIds": [str(BASELINE)], "candidateSourceIds": [str(CANDIDATE)], **extra}
    )


async def _evaluate(
    replies: list[httpx.Response | Exception], request: StageGateRequest | None = None
) -> tuple[list[httpx.Request], StageGateResult]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    async with _client(httpx.MockTransport(handler)) as client:
        result = await client.evaluate_stage_gate(
            request or _request(), deadline=Deadline.start(30, lambda: T0)
        )
    return seen, result


async def test_spec030_r19_posts_request_body_with_caller() -> None:
    """[SPEC-030 R-19] 내부 경로에 계약 이름(camelCase) 본문과 X-Caller를 싣는다."""
    request = _request(stageIds=["analyzer.judge"], maxRegressionRatio=0.3, minSamples=7)
    seen, result = await _evaluate([httpx.Response(200, json=RESULT)], request)

    [sent] = seen
    assert (sent.method, sent.url.path) == ("POST", "/internal/v1/quality/stage-gates")
    assert sent.headers["X-Caller"] == "quality-eval"
    assert json.loads(sent.content) == {
        "baselineSourceIds": [str(BASELINE)],
        "candidateSourceIds": [str(CANDIDATE)],
        "stageIds": ["analyzer.judge"],
        "maxRegressionRatio": 0.3,
        "minSamples": 7,
    }
    assert result == StageGateResult.model_validate(RESULT)


async def test_spec030_r19_omits_unset_optional_fields_and_deadline_header() -> None:
    """[SPEC-030 R-19] 선택 필드를 비우면 보내지 않고, 계약에 없는 마감 헤더도 보내지 않는다."""
    request = StageGateRequest.model_validate(
        {
            "baselineSourceIds": [str(BASELINE)],
            "candidateSourceIds": [str(CANDIDATE)],
            "maxRegressionRatio": None,
            "minSamples": None,
        }
    )
    seen, _ = await _evaluate([httpx.Response(200, json=RESULT)], request)

    [sent] = seen
    assert json.loads(sent.content) == {
        "baselineSourceIds": [str(BASELINE)],
        "candidateSourceIds": [str(CANDIDATE)],
    }
    assert "X-Request-Deadline" not in sent.headers


@pytest.mark.parametrize(
    "connection_failure",
    [httpx.ConnectError("refused"), httpx.RemoteProtocolError("reset")],
    ids=["connect", "disconnected"],
)
async def test_spec030_r19_retries_503_and_connection_failure_twice(
    connection_failure: Exception,
) -> None:
    """[SPEC-030 R-19] 저장하지 않는 계산이라 503과 연결 실패를 2회까지 다시 부른다."""
    replies: list[httpx.Response | Exception] = [
        _problem(503, "QUALITY_UNAVAILABLE"),
        connection_failure,
        httpx.Response(200, json=RESULT),
    ]
    seen, result = await _evaluate(replies)

    assert len(seen) == 3
    assert result.passed is False


async def test_spec030_r19_gives_up_after_two_retries() -> None:
    """[SPEC-030 R-19] 재시도 2회를 다 쓰면 마지막 실패로 끝난다."""
    replies: list[httpx.Response | Exception] = [
        _problem(503, "QUALITY_UNAVAILABLE"),
        _problem(503, "QUALITY_UNAVAILABLE"),
        httpx.ConnectError("refused"),
        httpx.Response(200, json=RESULT),
    ]

    with pytest.raises(QualityApiError) as raised:
        await _evaluate(replies)

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (
        "QUALITY_CONNECTION_FAILED",
        None,
        True,
    )
    assert len(replies) == 1


@pytest.mark.parametrize(
    ("reply", "code", "status"),
    [
        (_problem(400, "QUALITY_INVALID_REQUEST"), "QUALITY_INVALID_REQUEST", 400),
        (_problem(403, "QUALITY_CALLER_NOT_ALLOWED"), "QUALITY_CALLER_NOT_ALLOWED", 403),
        (httpx.Response(500, text="boom"), "QUALITY_HTTP_500", 500),
        (httpx.Response(200, json={"passed": True}), "QUALITY_INVALID_RESPONSE", 200),
        (httpx.ReadTimeout("slow"), "QUALITY_TIMEOUT", None),
    ],
    ids=["400", "403", "500", "invalid-body", "timeout"],
)
async def test_spec030_r19_other_failures_end_at_once_with_a_code(
    reply: httpx.Response | Exception, code: str, status: int | None
) -> None:
    """[SPEC-030 R-19] 400·403·500·계약과 맞지 않는 응답·응답 시간 초과는 다시 부르지 않는다."""
    replies: list[httpx.Response | Exception] = [reply, httpx.Response(200, json=RESULT)]

    with pytest.raises(QualityApiError) as raised:
        await _evaluate(replies)

    assert (raised.value.code, raised.value.status, raised.value.retryable) == (code, status, False)
    assert len(replies) == 1
