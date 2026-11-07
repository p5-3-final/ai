"""stream-service 내부 클라이언트(계약 `openapi/stream-service.yaml`, SPEC-027 R-33).

aggregator가 STREAM 세션의 구역을 세션당 한 번 읽는다. 읽기 전용이라 503과 연결 실패만 다시
시도한다(`READ_ONLY`, SPEC-021 R-10). 모델은 계약에서 생성한 `vlm_stream_client.models`만 쓴다.
"""

import asyncio
import time
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx
from pydantic import ValidationError
from vlm_common.http_call import (
    READ_ONLY,
    CallFailedError,
    CallRetry,
    Clock,
    Deadline,
    Failure,
    Sleep,
    call_with_retry,
    failure_of,
)

from vlm_stream_client.models import ProblemDetail, ZoneList

__all__ = ["StreamApiClient", "StreamApiError"]


class StreamApiError(CallFailedError):
    """stream-service 호출이 결과 없이 끝났다.

    Attributes:
        code: ProblemDetail의 `code`. 없으면 `STREAM_HTTP_<상태>`, 응답 대기 시간 초과는
            `STREAM_TIMEOUT`, 연결 실패는 `STREAM_CONNECTION_FAILED`, 계약과 맞지 않는 응답은
            `STREAM_INVALID_RESPONSE`다.
        status: HTTP 상태. 응답을 받지 못했으면 None이다.
        retryable: 다시 시도할 수 있는 실패였는지.
    """

    def __init__(
        self, code: str, message: str, *, status: int | None, retryable: bool, failure: Failure
    ) -> None:
        super().__init__(failure, message)
        self.code = code
        self.status = status
        self.retryable = retryable


class StreamApiClient:
    """stream-service 내부 읽기 클라이언트. `async with`로 연결을 닫는다.

    Args:
        base_url: stream-service 내부 주소.
        caller: `X-Caller` 헤더 값.
        timeout_s: 시도 하나의 제한 시간 상한.
        retry: 재시도 횟수와 지수 대기.
        transport: 테스트가 바꿔 넣는 전송 계층.
        sleep: 재시도 전 대기 함수.
        clock: epoch 초 시계.
    """

    def __init__(
        self,
        base_url: str,
        *,
        caller: str,
        timeout_s: float,
        retry: CallRetry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.time,
    ) -> None:
        self._timeout_s = timeout_s
        self._retry = retry or CallRetry()
        self._sleep = sleep
        self._clock = clock
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"X-Caller": caller},
            timeout=timeout_s,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_session_zones(self, session_id: UUID, *, deadline: Deadline) -> ZoneList:
        """세션 시작 때의 구역 목록(`getSessionZonesInternal`).

        Raises:
            StreamApiError: 오류 응답, 재시도 끝의 503·연결 실패, 시간 초과, 계약과 맞지 않는 응답.
        """
        path = f"/internal/v1/stream/sessions/{session_id}/zones"
        headers = deadline.header()

        async def attempt(timeout_s: float) -> ZoneList:
            return await self._attempt(path, headers, timeout_s)

        return await call_with_retry(
            attempt,
            deadline=deadline,
            timeout_s=self._timeout_s,
            rule=READ_ONLY,
            retry=self._retry,
            sleep=self._sleep,
            clock=self._clock,
        )

    async def _attempt(self, path: str, headers: dict[str, str], timeout_s: float) -> ZoneList:
        try:
            response = await self._http.get(path, headers=headers, timeout=max(timeout_s, 0.001))
        except httpx.TransportError as e:
            failure = failure_of(e)
            code = "STREAM_TIMEOUT" if failure.kind == "timeout" else "STREAM_CONNECTION_FAILED"
            raise StreamApiError(
                code, str(e), status=None, retryable=READ_ONLY(failure), failure=failure
            ) from e
        status = response.status_code
        if not response.is_success:
            failure = Failure("status", status)
            raise StreamApiError(
                _problem_code(response) or f"STREAM_HTTP_{status}",
                f"stream-service returned {status}",
                status=status,
                retryable=READ_ONLY(failure),
                failure=failure,
            )
        try:
            return ZoneList.model_validate_json(response.content)
        except ValidationError as e:
            raise StreamApiError(
                "STREAM_INVALID_RESPONSE",
                "stream-service response does not match the contract",
                status=status,
                retryable=False,
                failure=Failure("invalid", status),
            ) from e


def _problem_code(response: httpx.Response) -> str | None:
    try:
        problem = ProblemDetail.model_validate_json(response.content)
    except ValidationError:
        return None
    return problem.code or None
