"""내부 동기 호출(REST) 공통 규칙: 마감 헤더, 실패 분류, 호출 종류별 재시도.

docs 아키텍처 "통신 규칙"의 내부 동기 호출 규약(SPEC-021)이다. 호출자는 첫 시도 때 마감을 한 번
정해 모든 시도에 `X-Request-Deadline`(epoch ms)으로 싣는다. 읽기 전용 호출(`READ_ONLY`)은
503과 연결 실패에, 결과가 비용이 되는 호출(`NON_IDEMPOTENT`)은 요청이 닿지 않은 연결 실패에만
다시 시도한다. ai-gateway·analysis-api·stream-service 클라이언트가 같은 규칙을 쓴다.

사용 예::

    deadline = Deadline.start(timeout_s, clock)

    async def attempt(attempt_timeout_s: float) -> Response:
        ...  # httpx 호출. 실패는 CallFailedError(또는 하위 클래스)로 던진다.

    await call_with_retry(attempt, deadline=deadline, timeout_s=timeout_s,
                          rule=READ_ONLY, retry=CallRetry())
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Self

import httpx

from vlm_common.errors import VlmError

logger = logging.getLogger(__name__)

DEADLINE_HEADER = "X-Request-Deadline"
"""호출자가 기다리는 마지막 시각(epoch ms)을 싣는 요청 헤더."""

type FailureKind = Literal["connect", "disconnected", "timeout", "status", "invalid"]
"""시도 하나가 실패한 방식.

- `connect`: 연결하지 못해 요청을 보내지 않았다(연결 거부·연결 시간 초과·연결 풀 대기 초과).
- `disconnected`: 연결한 뒤 끊겼다. 요청 일부나 전부가 갔을 수 있다.
- `timeout`: 보낸 뒤 응답을 제한 시간 안에 받지 못했다.
- `status`: 성공이 아닌 HTTP 상태를 받았다.
- `invalid`: 성공 상태인데 본문이 계약과 맞지 않는다.
"""

type Clock = Callable[[], float]
"""epoch 초를 돌려주는 시계. 테스트가 바꿔 넣는다."""

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Failure:
    """시도 하나의 실패. `status`는 `kind`가 `status`·`invalid`일 때의 HTTP 상태다."""

    kind: FailureKind
    status: int | None = None


type RetryRule = Callable[[Failure], bool]
"""실패한 시도를 다시 해도 되는지 정하는 함수."""


def _not_connected(failure: Failure) -> bool:
    return failure.kind == "connect"


def _read_only(failure: Failure) -> bool:
    if failure.kind in ("connect", "disconnected"):
        return True
    return failure.kind == "status" and failure.status == 503


NON_IDEMPOTENT: RetryRule = _not_connected
"""결과가 비용·부작용인 호출(analyze VLM 판정). 요청이 닿지 않은 연결 실패만 다시 한다(R-9)."""

READ_ONLY: RetryRule = _read_only
"""읽기 전용 호출(embed·parse·analysis-api 읽기). 503과 연결 실패를 다시 한다(SPEC-021 R-10)."""


@dataclass(frozen=True, slots=True)
class CallRetry:
    """재시도 설정.

    Attributes:
        max_retries: 첫 시도 뒤 다시 부르는 최대 횟수. 규약 기본값은 2다.
        base_delay_s: 첫 재시도 전 대기. 재시도마다 두 배로 는다.
    """

    max_retries: int = 2
    base_delay_s: float = 1.0

    def __post_init__(self) -> None:
        if self.max_retries < 0 or self.base_delay_s < 0:
            raise ValueError("retry settings must not be negative")

    def delay_s(self, retry: int) -> float:
        """`retry`번째(1부터) 재시도 전 대기 시간."""
        return self.base_delay_s * 2.0 ** (retry - 1)


@dataclass(frozen=True, slots=True)
class Deadline:
    """호출 하나의 마감. 재시도해도 바뀌지 않는다(SPEC-021 R-8)."""

    at_ms: int

    @classmethod
    def start(cls, timeout_s: float, clock: Clock = time.time) -> Self:
        """지금(첫 시도 시각)부터 `timeout_s` 뒤를 마감으로 정한다."""
        return cls(int((clock() + timeout_s) * 1000))

    def remaining_s(self, clock: Clock = time.time) -> float:
        """마감까지 남은 초. 지났으면 0 이하다."""
        return self.at_ms / 1000 - clock()

    def header(self) -> dict[str, str]:
        """요청에 싣는 `X-Request-Deadline` 헤더."""
        return {DEADLINE_HEADER: str(self.at_ms)}


class CallFailedError(VlmError):
    """시도 하나가 실패했다. 클라이언트별 오류는 이 클래스를 상속해 코드를 정한다.

    Attributes:
        failure: 실패 방식. 재시도 규칙이 이 값을 본다.
    """

    def __init__(self, failure: Failure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


def failure_of(error: httpx.TransportError) -> Failure:
    """httpx 전송 오류를 실패 방식으로 나눈다.

    연결 시간 초과·연결 풀 대기 초과는 요청을 보내기 전이라 `connect`다. 쓰기 중 끊김은 요청 일부가
    갔을 수 있어 `disconnected`다.
    """
    if isinstance(error, httpx.ConnectError | httpx.ConnectTimeout | httpx.PoolTimeout):
        return Failure("connect")
    if isinstance(error, httpx.TimeoutException):
        return Failure("timeout")
    return Failure("disconnected")


async def call_with_retry[T](
    attempt: Callable[[float], Awaitable[T]],
    *,
    deadline: Deadline,
    timeout_s: float,
    rule: RetryRule,
    retry: CallRetry,
    sleep: Sleep = asyncio.sleep,
    clock: Clock = time.time,
) -> T:
    """`attempt`를 규칙대로 다시 부른다.

    Args:
        attempt: 시도 하나. 인자는 그 시도의 제한 시간(초)으로
            min(`timeout_s`, 마감까지 남은 시간)이다. 실패는 `CallFailedError`(또는 하위
            클래스)로 던진다. 다른 예외는 그대로 전달한다.
        deadline: 모든 시도에 같은 마감.
        timeout_s: 시도 하나의 제한 시간 상한.
        rule: 실패를 다시 시도할지 정하는 규칙(`NON_IDEMPOTENT`, `READ_ONLY`).
        retry: 횟수와 지수 대기.
        sleep: 대기 함수. 테스트가 바꿔 넣는다.
        clock: 시계. 테스트가 바꿔 넣는다.

    Raises:
        CallFailedError: 다시 시도할 수 없는 실패, 횟수를 다 쓴 실패, 또는 다음 대기가 마감에 닿는
            실패(SPEC-021 R-11). 마지막 시도의 오류다.
    """
    retries = 0
    while True:
        try:
            return await attempt(min(timeout_s, deadline.remaining_s(clock)))
        except CallFailedError as e:
            if not rule(e.failure) or retries >= retry.max_retries:
                raise
            delay = retry.delay_s(retries + 1)
            # 대기가 끝나는 시각에 마감이 이미 지났으면 시도할 시간이 없다(R-11).
            if delay >= deadline.remaining_s(clock):
                raise
            retries += 1
            logger.warning(
                "internal call failed, retrying",
                extra={
                    "failure": e.failure.kind,
                    "status": e.failure.status,
                    "attempt": retries + 1,
                    "delay_s": delay,
                    "remaining_s": round(deadline.remaining_s(clock), 3),
                },
            )
            await sleep(delay)
