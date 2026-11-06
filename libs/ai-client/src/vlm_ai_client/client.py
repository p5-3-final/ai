"""ai-gateway REST 클라이언트(계약 `openapi/ai-gateway.yaml`).

내부 동기 호출은 REST다(docs ADR-0002). 일시 오류의 재시도는 여기서 해서 워커마다 같은 규칙을 쓴다.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Self
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from vlm_common.errors import VlmError

logger = logging.getLogger(__name__)

# ai-gateway가 공급자 장애로 돌려주는 상태. 잠시 뒤 다시 부르면 성공할 수 있다(SPEC-020 R-11).
_TRANSIENT_STATUSES = frozenset({502, 503})


@dataclass(frozen=True, slots=True)
class CallRetry:
    """일시 오류의 재시도 설정.

    Attributes:
        max_retries: 첫 시도 뒤 다시 부르는 최대 횟수.
        base_delay_s: 첫 재시도 전 대기. 재시도마다 두 배로 는다.
    """

    max_retries: int = 2
    base_delay_s: float = 1.0

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.base_delay_s < 0:
            raise ValueError("retry settings must not be negative")


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)


class AnalyzeImage(_Body):
    """판정에 보낼 이미지(블러 사본) 경로. 보낸 순서가 모델에 보이는 순서다."""

    storage_key: str = Field(min_length=1, max_length=1024)
    pts_sec: float | None = Field(default=None, ge=0, exclude_if=lambda v: v is None)


class AnalyzeRequest(_Body):
    """`POST /v1/analyze` 본문."""

    # taskType은 열린 목록이라 문자열로 둔다(docs ADR-0014).
    task_type: str
    source_id: UUID
    images: list[AnalyzeImage]
    variables: dict[str, object]


class AnalyzeResponse(_Body):
    """`POST /v1/analyze` 응답. `output`의 형식은 taskType의 출력 스키마다."""

    # 계약은 필드 추가만 허용하므로 모르는 필드는 무시한다.
    model_config = ConfigDict(extra="ignore")

    call_id: UUID
    task_type: str
    model_id: str | None
    prompt_version: str
    taxonomy_version: str | None
    output: dict[str, object]


class AiGatewayError(VlmError):
    """ai-gateway 호출이 결과 없이 끝났다.

    Attributes:
        code: ProblemDetail의 `code`. 응답에 없으면 `AI_HTTP_<상태>`, 제한 시간 초과는
            `AI_TIMEOUT`, 연결 실패는 `AI_CONNECTION_FAILED`, 계약과 맞지 않는 응답은
            `AI_INVALID_RESPONSE`다.
        status: HTTP 상태. 응답을 받지 못했으면 None이다.
        retryable: 일시 오류인지. True면 재시도를 다 쓰고 실패한 것이다.
    """

    def __init__(self, code: str, message: str, *, status: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable


class AiGatewayClient:
    """ai-gateway REST 클라이언트. `async with`로 연결을 닫는다.

    Args:
        base_url: ai-gateway 주소(예: `http://ai-gateway:8000`).
        caller: `X-Caller` 헤더에 싣는 서비스 이름. ai-gateway가 허용 목록과 비교한다.
        timeout_s: 호출 한 번의 제한 시간. ai-gateway의 모델 제한 시간보다 길게 잡는다.
        retry: 502·503·시간 초과·연결 실패의 재시도 설정.
        transport: 테스트가 바꿔 넣는 전송 계층.
        sleep: 재시도 전 대기 함수. 테스트가 바꿔 넣는다.
    """

    def __init__(
        self,
        base_url: str,
        *,
        caller: str,
        timeout_s: float,
        retry: CallRetry | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._retry = retry or CallRetry()
        self._sleep = sleep
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
        """이미지와 입력 변수로 taskType 하나를 판정한다.

        Raises:
            AiGatewayError: 오류 응답을 받았거나, 일시 오류가 재시도 끝에도 이어졌거나, 응답이
                계약과 맞지 않을 때.
        """
        headers = {"traceparent": traceparent} if traceparent else {}
        body = request.model_dump(mode="json", by_alias=True)
        attempt = 0
        while True:
            try:
                response = await self._post("/v1/analyze", body, headers)
                return _parsed(response, AnalyzeResponse)
            except AiGatewayError as e:
                if not e.retryable or attempt >= self._retry.max_retries:
                    raise
                delay = self._retry.base_delay_s * 2**attempt
                attempt += 1
                logger.warning(
                    "ai-gateway call failed, retrying",
                    extra={"code": e.code, "attempt": attempt, "delay_s": delay},
                )
                await self._sleep(delay)

    async def _post(
        self, path: str, body: dict[str, object], headers: dict[str, str]
    ) -> httpx.Response:
        try:
            response = await self._http.post(path, json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise AiGatewayError("AI_TIMEOUT", str(e), status=None, retryable=True) from e
        except httpx.TransportError as e:
            raise AiGatewayError("AI_CONNECTION_FAILED", str(e), status=None, retryable=True) from e
        if response.is_success:
            return response
        status = response.status_code
        raise AiGatewayError(
            _problem_code(response) or f"AI_HTTP_{status}",
            f"ai-gateway returned {status}",
            status=status,
            retryable=status in _TRANSIENT_STATUSES,
        )


def _problem_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and code else None


def _parsed[M: BaseModel](response: httpx.Response, model: type[M]) -> M:
    try:
        return model.model_validate_json(response.content)
    except ValidationError as e:
        # 같은 응답이 또 올 것이라 재시도하지 않는다.
        raise AiGatewayError(
            "AI_INVALID_RESPONSE",
            "ai-gateway response does not match the contract",
            status=response.status_code,
            retryable=False,
        ) from e
