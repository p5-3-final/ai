"""호출자 마감 `X-Request-Deadline`(SPEC-021 R-1~R-5).

마감은 호출자 시계의 epoch ms이고 비교는 이 Pod의 시계로 한다. 같은 클러스터의 NTP 오차(수 ms)는
무시한다.
"""

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ai_gateway.errors import InvalidRequestError

HEADER = "X-Request-Deadline"
# 계약 parameter의 pattern과 같다. 17자리 이상은 epoch ms로 수천 년 뒤라 형식 오류로 본다.
_EPOCH_MS = re.compile(r"[0-9]{1,16}")

type Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Deadline:
    """요청 하나의 마감. 남은 시간은 물을 때마다 다시 계산한다.

    그래서 이미지 읽기·캐시 확인에 쓴 시간이 외부 모델 제한 시간에서 빠진다.
    """

    at_ms: int
    clock: Clock = field(default=time.time, compare=False, repr=False)

    def remaining_s(self) -> float:
        """마감까지 남은 초. 지났으면 0 이하다."""
        return self.at_ms / 1000 - self.clock()

    def expired(self) -> bool:
        return self.remaining_s() <= 0

    def lag_ms(self) -> int:
        """마감을 넘긴 밀리초. 지나지 않았으면 0 이하다."""
        return int(self.clock() * 1000) - self.at_ms

    def cap(self, timeout_s: float) -> float:
        """프로필 제한 시간과 남은 시간 중 짧은 것(R-1)."""
        return min(timeout_s, self.remaining_s())


def cap(deadline: Deadline | None, timeout_s: float) -> float:
    """마감이 없으면 프로필 값 그대로다(R-5)."""
    return timeout_s if deadline is None else deadline.cap(timeout_s)


def parse_deadline_header(value: str | None, clock: Clock = time.time) -> Deadline | None:
    """헤더 값을 마감으로 바꾼다. 없으면 None이다(R-5).

    Raises:
        InvalidRequestError: 0 이상의 정수(epoch ms)가 아닐 때(R-4).
    """
    if value is None:
        return None
    if not _EPOCH_MS.fullmatch(value):
        raise InvalidRequestError(
            f"{HEADER} must be epoch milliseconds", [(HEADER, "must be epoch milliseconds")]
        )
    return Deadline(int(value), clock)
