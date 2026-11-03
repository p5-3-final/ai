"""재시도 횟수와 지연(지수 백오프 + 지터) 정책."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """메시지 처리 재시도 정책.

    Attributes:
        max_attempts: 첫 시도를 포함한 최대 시도 횟수. 넘으면 DLQ로 보낸다.
        base_delay_s: 두 번째 시도 전 지연의 상한(초). 시도마다 두 배가 된다.
        max_delay_s: 지연의 최댓값(초).
    """

    max_attempts: int = 5
    base_delay_s: float = 1.0
    max_delay_s: float = 300.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_s < 0 or self.max_delay_s < self.base_delay_s:
            raise ValueError("delays must satisfy 0 <= base_delay_s <= max_delay_s")

    def delay_before(self, attempt: int, rand: float) -> float:
        """`attempt`번째 시도(2 이상) 전에 기다릴 초.

        여러 Pod가 같은 시각에 몰려 재시도하지 않도록 지터를 넣되, 지연이 너무 짧아지지 않게
        상한의 절반 이상은 남긴다.

        Args:
            attempt: 곧 할 시도 번호.
            rand: [0, 1) 균등 난수.
        """
        ceiling = min(self.max_delay_s, self.base_delay_s * 2 ** (attempt - 2))
        return ceiling * (0.5 + 0.5 * rand)

    def exhausted(self, attempt: int) -> bool:
        """`attempt`번째 시도가 실패했을 때 더 시도하지 않아야 하면 True."""
        return attempt >= self.max_attempts
