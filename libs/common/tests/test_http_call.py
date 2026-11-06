"""내부 동기 호출 공통 규칙: 마감, 실패 분류, 호출 종류별 재시도(SPEC-021 R-8~R-11)."""

import httpx
import pytest
from vlm_common.http_call import (
    NON_IDEMPOTENT,
    READ_ONLY,
    CallFailedError,
    CallRetry,
    Deadline,
    Failure,
    call_with_retry,
    failure_of,
)

T0 = 1_762_000_000.0


class _Clock:
    def __init__(self) -> None:
        self.now = T0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_spec021_r8_deadline_is_start_plus_timeout_in_epoch_ms() -> None:
    clock = _Clock()

    deadline = Deadline.start(2.5, clock)

    assert deadline.header() == {"X-Request-Deadline": str(int(T0 * 1000) + 2500)}
    clock.now += 1
    assert deadline.remaining_s(clock) == pytest.approx(1.5)


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("x"), Failure("connect")),
        (httpx.ConnectTimeout("x"), Failure("connect")),
        (httpx.PoolTimeout("x"), Failure("connect")),
        (httpx.ReadTimeout("x"), Failure("timeout")),
        (httpx.WriteTimeout("x"), Failure("timeout")),
        (httpx.ReadError("x"), Failure("disconnected")),
        (httpx.WriteError("x"), Failure("disconnected")),
        (httpx.RemoteProtocolError("x"), Failure("disconnected")),
    ],
)
def test_spec021_r9_transport_errors_are_classified(
    error: httpx.TransportError, expected: Failure
) -> None:
    assert failure_of(error) == expected


@pytest.mark.parametrize(
    ("failure", "non_idempotent", "read_only"),
    [
        (Failure("connect"), True, True),
        (Failure("disconnected"), False, True),
        (Failure("timeout"), False, False),
        (Failure("status", 503), False, True),
        (Failure("status", 502), False, False),
        (Failure("status", 504), False, False),
        (Failure("invalid", 200), False, False),
    ],
)
def test_spec021_r9_r10_rules_by_call_kind(
    failure: Failure, non_idempotent: bool, read_only: bool
) -> None:
    assert (NON_IDEMPOTENT(failure), READ_ONLY(failure)) == (non_idempotent, read_only)


async def test_spec021_r8_attempt_timeout_is_capped_by_remaining_time() -> None:
    clock = _Clock()
    deadline = Deadline.start(3, clock)
    budgets: list[float] = []

    async def attempt(timeout_s: float) -> str:
        budgets.append(timeout_s)
        if len(budgets) == 1:
            raise CallFailedError(Failure("connect"), "refused")
        return "ok"

    result = await call_with_retry(
        attempt,
        deadline=deadline,
        timeout_s=3,
        rule=NON_IDEMPOTENT,
        retry=CallRetry(max_retries=2, base_delay_s=1),
        sleep=clock.sleep,
        clock=clock,
    )

    assert result == "ok"
    assert budgets == [pytest.approx(3), pytest.approx(2)]


async def test_spec021_r11_stops_when_next_wait_reaches_deadline() -> None:
    clock = _Clock()
    calls = 0

    async def attempt(timeout_s: float) -> str:
        nonlocal calls
        calls += 1
        raise CallFailedError(Failure("connect"), "refused")

    with pytest.raises(CallFailedError):
        await call_with_retry(
            attempt,
            deadline=Deadline.start(1, clock),
            timeout_s=1,
            rule=NON_IDEMPOTENT,
            retry=CallRetry(max_retries=5, base_delay_s=0.5),
            sleep=clock.sleep,
            clock=clock,
        )

    assert (calls, clock.sleeps) == (2, [0.5])


def test_retry_settings_must_not_be_negative() -> None:
    with pytest.raises(ValueError, match="negative"):
        CallRetry(max_retries=-1)
