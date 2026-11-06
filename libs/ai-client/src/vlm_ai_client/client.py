"""ai-gateway REST 클라이언트(계약 `openapi/ai-gateway.yaml`).

내부 동기 호출은 REST다(docs ADR-0002). 요청·응답 모델은 계약에서 생성한
`vlm_ai_client.models`만 쓰고(SPEC-021 R-6), 마감·재시도는 `vlm_common.http_call`의 규약을
따른다(SPEC-021 R-8~R-11).

| 호출 | 재시도 |
| --- | --- |
| `analyze` | 요청이 닿지 않은 연결 실패만(`NON_IDEMPOTENT`). VLM 비용이 두 번 들지 않게 한다 |
| `embed`, `parse` | 503과 연결 실패(`READ_ONLY`) |
"""

import asyncio
import time
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ValidationError
from vlm_common.http_call import (
    NON_IDEMPOTENT,
    READ_ONLY,
    CallFailedError,
    CallRetry,
    Clock,
    Deadline,
    Failure,
    RetryRule,
    Sleep,
    call_with_retry,
    failure_of,
)

from vlm_ai_client.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    EmbedRequest,
    EmbedResponse,
    ParseRequest,
    ParseResponse,
    ProblemDetail,
)

__all__ = ["AiGatewayClient", "AiGatewayError", "CallRetry"]


class AiGatewayError(CallFailedError):
    """ai-gateway 호출이 결과 없이 끝났다(SPEC-021 R-12).

    Attributes:
        code: ProblemDetail의 `code`. 응답에 없으면 `AI_HTTP_<상태>`, 응답 대기 시간 초과는
            `AI_TIMEOUT`, 연결 실패는 `AI_CONNECTION_FAILED`, 계약과 맞지 않는 응답은
            `AI_INVALID_RESPONSE`다.
        status: HTTP 상태. 응답을 받지 못했으면 None이다.
        retryable: 그 호출 종류에서 다시 시도할 수 있는 실패였는지. True면 재시도를 다 쓰거나
            마감에 닿아 끝난 것이다.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None,
        retryable: bool,
        failure: Failure | None = None,
    ) -> None:
        super().__init__(failure or _failure_from_status(status), message)
        self.code = code
        self.status = status
        self.retryable = retryable


class AiGatewayClient:
    """ai-gateway REST 클라이언트. `async with`로 연결을 닫는다.

    Args:
        base_url: ai-gateway 주소(예: `http://ai-gateway:8000`).
        caller: `X-Caller` 헤더에 싣는 서비스 이름. ai-gateway가 허용 목록과 비교한다.
        timeout_s: 호출 하나(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값이
            `X-Request-Deadline`이다. ai-gateway의 모델 제한 시간보다 길게 잡는다.
        retry: 재시도 횟수와 지수 대기. 어떤 실패를 다시 할지는 호출 종류가 정한다.
        transport: 테스트가 바꿔 넣는 전송 계층.
        sleep: 재시도 전 대기 함수. 테스트가 바꿔 넣는다.
        clock: epoch 초 시계. 테스트가 바꿔 넣는다.
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

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse:
        """이미지와 입력 변수로 taskType 하나를 판정한다. 연결 실패만 다시 시도한다(R-9).

        Raises:
            AiGatewayError: 오류 응답, 재시도 끝의 연결 실패, 시간 초과, 계약과 맞지 않는 응답.
        """
        return await self._call(
            "/v1/analyze", request, AnalyzeResponse, NON_IDEMPOTENT, traceparent
        )

    async def embed(
        self, request: EmbedRequest, *, traceparent: str | None = None
    ) -> EmbedResponse:
        """문장·이미지를 벡터로 바꾼다. 503과 연결 실패를 다시 시도한다(R-10, R-13).

        Raises:
            AiGatewayError: `analyze`와 같다.
        """
        return await self._call("/v1/embed", request, EmbedResponse, READ_ONLY, traceparent)

    async def parse(
        self, request: ParseRequest, *, traceparent: str | None = None
    ) -> ParseResponse:
        """검색 문장을 해석한다. 503과 연결 실패를 다시 시도한다(R-10, R-13).

        Raises:
            AiGatewayError: `analyze`와 같다.
        """
        return await self._call("/v1/parse", request, ParseResponse, READ_ONLY, traceparent)

    async def _call[M: BaseModel](
        self,
        path: str,
        request: BaseModel,
        model: type[M],
        rule: RetryRule,
        traceparent: str | None,
    ) -> M:
        deadline = Deadline.start(self._timeout_s, self._clock)
        headers = deadline.header()
        if traceparent:
            headers["traceparent"] = traceparent
        # 넣지 않은 선택 필드는 싣지 않는다. variables 안의 null은 값이라 그대로 둔다.
        body = request.model_dump(mode="json", by_alias=True, exclude_unset=True)

        async def attempt(timeout_s: float) -> M:
            return await self._attempt(path, body, headers, timeout_s, model, rule)

        return await call_with_retry(
            attempt,
            deadline=deadline,
            timeout_s=self._timeout_s,
            rule=rule,
            retry=self._retry,
            sleep=self._sleep,
            clock=self._clock,
        )

    async def _attempt[M: BaseModel](
        self,
        path: str,
        body: dict[str, object],
        headers: dict[str, str],
        timeout_s: float,
        model: type[M],
        rule: RetryRule,
    ) -> M:
        try:
            response = await self._http.post(path, json=body, headers=headers, timeout=timeout_s)
        except httpx.TransportError as e:
            failure = failure_of(e)
            code = "AI_TIMEOUT" if failure.kind == "timeout" else "AI_CONNECTION_FAILED"
            raise AiGatewayError(
                code, str(e), status=None, retryable=rule(failure), failure=failure
            ) from e
        status = response.status_code
        if not response.is_success:
            failure = Failure("status", status)
            raise AiGatewayError(
                _problem_code(response) or f"AI_HTTP_{status}",
                f"ai-gateway returned {status}",
                status=status,
                retryable=rule(failure),
                failure=failure,
            )
        try:
            return model.model_validate_json(response.content)
        except ValidationError as e:
            # 같은 응답이 또 올 것이라 재시도하지 않는다.
            raise AiGatewayError(
                "AI_INVALID_RESPONSE",
                "ai-gateway response does not match the contract",
                status=status,
                retryable=False,
                failure=Failure("invalid", status),
            ) from e


def _failure_from_status(status: int | None) -> Failure:
    # 테스트·호출자가 상태만으로 만든 오류. 상태가 없으면 응답을 받지 못한 실패로 본다.
    return Failure("status", status) if status is not None else Failure("disconnected")


def _problem_code(response: httpx.Response) -> str | None:
    try:
        problem = ProblemDetail.model_validate_json(response.content)
    except ValidationError:
        return None
    return problem.code or None
