"""처리 단계 측정(SPEC-013, docs overview "처리 단계 측정").

워커가 메시지마다 `StageRecorder`를 만들어 `MessageContext.timing`으로 넘긴다. 큐 대기와 핸들러
전체는 라이브러리가 재고, 서비스는 `ctx.timing.stage("<단계 ID>")` 블록으로 세부 단계를 잰다.
"""

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from vlm_contracts.events import Event
from vlm_contracts.stages import (
    StageId,
    StageStatus,
    StageSubject,
    StageTiming,
    StageTimingBatch,
)

from vlm_messaging.publisher import Headers

STAGE_TIMING_TOPIC = "stage.timing"
# 측정 기록 ID용 uuid5 이름공간. 바꾸면 같은 메시지를 다시 처리한 기록이 새 기록으로 쌓인다.
_NAMESPACE = uuid.UUID("6f1c2a8e-3b4d-5e6f-8a9b-0c1d2e3f4a5b")

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MessagePosition:
    """소비한 메시지의 위치. 묶음 ID에 쓴다."""

    topic: str
    partition: int
    offset: int


@dataclass
class StageBlock:
    """실행 중인 단계 블록. 처리한 개수는 블록 안에서 알게 되므로 여기에 채운다."""

    count: int | None = None


class StageRecorder:
    """메시지 하나를 처리하는 동안의 측정 기록.

    한 핸들러 안에서만 쓴다. 블록은 핸들러가 넘긴 스레드에서 열어도 되지만 동시에 여러 스레드에서
    열지 않는다(경로를 스택 하나로 잇는다).
    """

    def __init__(
        self,
        *,
        service: str | None,
        attempt: int,
        received_at: datetime,
        broker_appended_at: datetime | None = None,
        trace_id: str | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.perf_counter,
    ) -> None:
        """
        Args:
            service: 서비스 이름. `<service>.queue_wait`·`<service>.handle` 단계 ID가 된다. 워커
                밖에서 만든 기록기(테스트 등)는 None이고 세부 단계만 남긴다.
            attempt: 메시지 시도 번호.
            received_at: 메시지를 받은 시각.
            broker_appended_at: 브로커가 붙인 적재 시각(LogAppendTime). 토픽이 생산자 시각을 쓰면
                None이다.
            trace_id: 소비한 메시지의 W3C trace ID.
            wall: 시작·끝 시각에 쓰는 시계.
            monotonic: 걸린 시간에 쓰는 단조 시계. 벽시계가 조정돼도 시간이 틀어지지 않는다.
        """
        self._service = service
        self._attempt = attempt
        self._received_at = received_at
        self._broker_appended_at = broker_appended_at
        self._trace_id = trace_id
        self._wall = wall
        self._monotonic = monotonic
        self._subject = StageSubject()
        self._path: list[str] = []
        self.timings: list[StageTiming] = []

    def batch(
        self, *, service: str, pod: str, position: MessagePosition
    ) -> StageTimingBatch | None:
        """지금까지의 기록을 `stage.timing` 메시지 하나로 묶는다. 기록이 없으면 None(R-5).

        묶음 ID는 메시지 위치와 시도 번호로 정해, 같은 메시지를 같은 시도로 다시 처리하면 같다(R-7).
        """
        if not self.timings:
            return None
        where = f"{position.topic}|{position.partition}|{position.offset}"
        key = f"{self._service}|{where}|{self._attempt}"
        return StageTimingBatch(
            batch_id=uuid.uuid5(_NAMESPACE, key),
            service=service,
            pod=pod,
            produced_at=self._wall(),
            timings=self.timings,
        )

    def begin(self, event: Event) -> None:
        """소비한 이벤트로 측정 대상을 정하고 큐 대기(`<service>.queue_wait`)를 남긴다(R-1, R-2).

        보낸 시각은 브로커 적재 시각이 있으면 그것을 쓴다. 생산자 Pod와 이 Pod의 시계 차이가 빠지기
        때문이다. 없으면 봉투의 `producedAt`, 둘 다 없으면 큐 대기를 남기지 않는다.
        """
        self._subject = StageSubject(source_id=event.source_id)
        sent_at = self._broker_appended_at or event.produced_at
        if self._service is None or sent_at is None:
            return
        waited = self._received_at - sent_at
        self._add(
            cast(StageId, f"{self._service}.queue_wait"),
            f"{self._service}.queue_wait",
            sent_at,
            self._received_at,
            waited / timedelta(milliseconds=1),
            "ok",
        )

    @contextmanager
    def handle(self) -> Iterator[None]:
        """핸들러 전체(`<service>.handle`). 소비 그룹이 없으면 아무것도 남기지 않는다."""
        if self._service is None:
            yield
            return
        with self._block(cast(StageId, f"{self._service}.handle"), unit=None):
            yield

    @contextmanager
    def stage(self, stage_id: StageId, *, unit: str | None = None) -> Iterator[StageBlock]:
        """핸들러 안의 세부 단계(R-4). 예외로 끝나면 `error`로 남기고 예외를 그대로 전한다."""
        with self._block(stage_id, unit=unit) as block:
            yield block

    @contextmanager
    def _block(self, stage_id: StageId, *, unit: str | None) -> Iterator[StageBlock]:
        self._path.append(stage_id)
        path = "/".join(self._path)
        block = StageBlock()
        started_at, started = self._wall(), self._monotonic()
        status: StageStatus = "error"
        try:
            yield block
            status = "ok"
        finally:
            elapsed = self._monotonic() - started
            self._path.pop()
            self._add(
                stage_id,
                path,
                started_at,
                started_at + timedelta(seconds=elapsed),
                elapsed * 1000,
                status,
                count=block.count,
                unit=unit,
            )

    def _add(
        self,
        stage_id: StageId,
        path: str,
        started_at: datetime,
        ended_at: datetime,
        duration_ms: float,
        status: StageStatus,
        *,
        count: int | None = None,
        unit: str | None = None,
    ) -> None:
        # 핸들러 안 순번을 넣어 같은 단계가 여러 번 돌아도 기록이 겹치지 않게 한다(R-7).
        subject = self._subject.model_dump_json(by_alias=True)
        key = f"{path}|{subject}|{self._attempt}|{len(self.timings)}"
        self.timings.append(
            StageTiming(
                timing_id=uuid.uuid5(_NAMESPACE, key),
                stage_id=stage_id,
                path=path,
                subject=self._subject,
                attempt=self._attempt,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                count=count,
                unit=unit,
                status=status,
                trace_id=self._trace_id,
            )
        )


class TimingSink(Protocol):
    """측정 묶음을 내보내는 곳(R-5). 보내지 못하면 `VlmError`(발행 실패는 `PublishError`)를 던지고,
    워커는 경고만 남긴다(R-6).
    """

    def emit(self, batch: StageTimingBatch, *, traceparent: str | None) -> None: ...


class RawPublisher(Protocol):
    def send_raw(
        self, topic: str, key: str | bytes | None, value: bytes, headers: Headers
    ) -> None: ...


class LogTimingSink:
    """발행 설정을 끈 서비스(로컬 기본)의 출력기. 묶음 하나를 JSON 로그 한 줄로 남긴다."""

    def emit(self, batch: StageTimingBatch, *, traceparent: str | None) -> None:
        logger.info(
            "stage timing", extra={"stageTiming": batch.model_dump(mode="json", by_alias=True)}
        )


class KafkaTimingSink:
    """묶음을 `stage.timing`에 발행한다. 키는 측정 대상의 sourceId, 없으면 묶음 ID다."""

    def __init__(self, publisher: RawPublisher, *, topic: str = STAGE_TIMING_TOPIC) -> None:
        self._publisher = publisher
        self.topic = topic

    def emit(self, batch: StageTimingBatch, *, traceparent: str | None) -> None:
        sources = [t.subject.source_id for t in batch.timings if t.subject.source_id is not None]
        key = str(sources[0]) if sources else str(batch.batch_id)
        headers = [("traceparent", traceparent.encode())] if traceparent else []
        self._publisher.send_raw(
            self.topic, key, batch.model_dump_json(by_alias=True).encode(), headers
        )
