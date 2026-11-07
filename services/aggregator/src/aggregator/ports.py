"""핸들러가 함께 쓰는 설정·오류·발행 인터페이스."""

from dataclasses import dataclass
from typing import Protocol

from vlm_common.errors import VlmError
from vlm_contracts.events import Event


class CountMismatchError(VlmError):
    """읽은 `rule*` 이벤트·트랙 수가 `counts`와 다르다.

    적재가 덜 끝났을 수 있어 다시 시도한다(R-4, R-30).
    """

    code = "AGGREGATOR_COUNT_MISMATCH"


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    """핸들러 설정.

    Attributes:
        read_timeout_s: 한 메시지의 내부 읽기 마감(핸들러 시작 + 이 값, R-3, R-33).
        max_attempts: 워커 재시도 정책의 최대 시도 수. 이 시도에서도 개수가 다르면 partial로
            발행한다(R-4, R-30). 워커의 `RetryPolicy.max_attempts`와 같아야 한다.
    """

    read_timeout_s: float = 300
    max_attempts: int = 5
