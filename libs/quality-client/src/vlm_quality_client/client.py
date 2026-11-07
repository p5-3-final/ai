"""quality-service 내부 클라이언트(계약 `openapi/quality-service.yaml`, SPEC-030 R-19).

평가 잡이 처리 단계 성능 Gate(`evaluateStageGate`)를 부른다. 저장하지 않는 계산(SPEC-030 R-18)이라
읽기 전용 규칙으로 503과 연결 실패만 다시 시도한다(`READ_ONLY`, SPEC-021 R-10). 모델은 계약에서
생성한 `vlm_quality_client.models`만 쓴다.
"""

import asyncio
import time
from types import TracebackType
from typing import Self

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

from vlm_quality_client.models import ProblemDetail, StageGateRequest, StageGateResult

_STAGE_GATES_PATH = "/internal/v1/quality/stage-gates"

__all__ = ["QualityApiClient", "QualityApiError"]


class QualityApiError(CallFailedError):
    """quality-service 호출이 결과 없이 끝났다.

    Attributes:
        code: ProblemDetail의 `code`. 없으면 `QUALITY_HTTP_<상태>`, 응답 대기 시간 초과는
            `QUALITY_TIMEOUT`, 연결 실패는 `QUALITY_CONNECTION_FAILED`, 계약과 맞지 않는 응답은
            `QUALITY_INVALID_RESPONSE`다.
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


class QualityApiClient:
    """quality-service 내부 클라이언트. `async with`로 연결을 닫는다.

    Args:
        base_url: quality-service 내부 주소.
        caller: `X-Caller` 헤더 값. quality-service 허용 호출자 목록에 있어야 한다.
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

    async def evaluate_stage_gate(
        self, request: StageGateRequest, *, deadline: Deadline
    ) -> StageGateResult:
        """처리 단계 성능 Gate(`evaluateStageGate`).

        `X-Request-Deadline`은 보내지 않는다. quality-service 계약에 없는 헤더라서다(plan 5절 2).
        `deadline`은 재시도가 마감을 넘지 않게 하는 데만 쓴다.

        Raises:
            QualityApiError: 오류 응답, 재시도 끝의 503·연결 실패, 시간 초과, 계약과 맞지 않는 응답.
        """
        # 비운 선택 필드(None)는 계약이 null을 받지 않으므로 빼고 보낸다. 서버 기본값이 쓰인다.
        body = request.model_dump(mode="json", by_alias=True, exclude_none=True)

        async def attempt(timeout_s: float) -> StageGateResult:
            return await self._attempt(body, timeout_s)

        return await call_with_retry(
            attempt,
            deadline=deadline,
            timeout_s=self._timeout_s,
            rule=READ_ONLY,
            retry=self._retry,
            sleep=self._sleep,
            clock=self._clock,
        )

    async def _attempt(self, body: dict[str, object], timeout_s: float) -> StageGateResult:
        try:
            response = await self._http.post(
                _STAGE_GATES_PATH, json=body, timeout=max(timeout_s, 0.001)
            )
        except httpx.TransportError as e:
            failure = failure_of(e)
            code = "QUALITY_TIMEOUT" if failure.kind == "timeout" else "QUALITY_CONNECTION_FAILED"
            raise QualityApiError(
                code, str(e), status=None, retryable=READ_ONLY(failure), failure=failure
            ) from e
        status = response.status_code
        if not response.is_success:
            failure = Failure("status", status)
            raise QualityApiError(
                _problem_code(response) or f"QUALITY_HTTP_{status}",
                f"quality-service returned {status}",
                status=status,
                retryable=READ_ONLY(failure),
                failure=failure,
            )
        try:
            return StageGateResult.model_validate_json(response.content)
        except ValidationError as e:
            raise QualityApiError(
                "QUALITY_INVALID_RESPONSE",
                "quality-service response does not match the contract",
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
