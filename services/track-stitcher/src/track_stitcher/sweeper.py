"""대기 제한 시간 주기 확인(SPEC-022 R-15, R-16).

Pod마다 별도 스레드에서 돌고, 확정은 확정 임대를 얻은 Pod 하나만 한다. 리더 선출 없이도 Pod가 죽으면
임대가 끝난 뒤 다른 Pod가 이어 받는다(plan 5절).
"""

import logging
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from vlm_common.errors import VlmError
from vlm_contracts.events import Event
from vlm_messaging.timing import MessagePosition, StageRecorder, TimingSink

from track_stitcher.service import StitcherService
from track_stitcher.state import JobKey, JobStateStore

SERVICE_NAME = "track-stitcher"
# 측정 묶음 ID를 정하는 자리 위치. 메시지가 아니라서 토픽 대신 이 이름과 jobId를 쓴다.
_SWEEP_POSITION_TOPIC = "track-stitcher.sweep"

logger = logging.getLogger(__name__)


class Sweeper:
    """`sweep_once`를 `interval_s`마다 부른다.

    Args:
        store: 대기 제한 시각을 가진 상태 저장소.
        service: 확정을 맡는 서비스.
        interval_s: 확인 간격.
        timing: 핸들러 밖 확정의 단계 측정을 내보내는 곳(plan 8절 7번).
        clock: epoch 초 시계.
        batch: 한 번에 꺼내는 최대 Job 수.
    """

    def __init__(
        self,
        store: JobStateStore,
        service: StitcherService,
        *,
        interval_s: float,
        timing: TimingSink,
        clock: Callable[[], float] = time.time,
        batch: int = 100,
    ) -> None:
        self._store = store
        self._service = service
        self._interval_s = interval_s
        self._timing = timing
        self._clock = clock
        self._batch = batch
        self._pod = socket.gethostname()

    def sweep_once(self) -> int:
        """대기 제한 시간이 지난 Job을 확정하고 확정한 수를 돌려준다.

        한 Job의 실패(저장소 장애 등)는 로그만 남기고 다음 Job으로 넘어간다. 그 Job은 확정 임대를
        풀어 다음 주기에 다시 확정한다.
        """
        now_ms = int(self._clock() * 1000)
        finalized = 0
        for key in self._store.due(now_ms, self._batch):
            recorder = self._recorder(key)
            try:
                finalized += self._service.finalize_due(key, recorder)
            except Exception:
                logger.exception(
                    "sweep finalize failed",
                    extra={"sourceId": str(key.source_id), "jobId": str(key.job_id)},
                )
            finally:
                self._emit(key, recorder)
        return finalized

    def run(self, stop: threading.Event) -> None:
        """`stop`이 설정될 때까지 확인을 반복한다. 상태 저장소 장애에도 스레드는 죽지 않는다."""
        while not stop.wait(self._interval_s):
            try:
                self.sweep_once()
            except Exception:
                logger.exception("sweep failed")

    def _recorder(self, key: JobKey) -> StageRecorder:
        now = datetime.fromtimestamp(self._clock(), UTC)
        recorder = StageRecorder(service=None, attempt=1, received_at=now)
        # 측정 대상(sourceId)만 정하려는 자리 이벤트다. 소비 그룹이 없으므로 큐 대기는 남지 않는다.
        recorder.begin(
            Event(
                event_id=uuid5(NAMESPACE_URL, f"vlm:track-stitcher.sweep:{key.job_id}"),
                event_type=_SWEEP_POSITION_TOPIC,
                schema_version=1,
                occurred_at=now,
                source_id=key.source_id,
                owner_id=key.source_id,
            )
        )
        return recorder

    def _emit(self, key: JobKey, recorder: StageRecorder) -> None:
        # 같은 Job을 다시 확정하면 같은 묶음 ID라 소비자가 중복을 흡수한다.
        position = MessagePosition(_SWEEP_POSITION_TOPIC, 0, key.job_id.int % (1 << 62))
        batch = recorder.batch(service=SERVICE_NAME, pod=self._pod, position=position)
        if batch is None:
            return
        try:
            self._timing.emit(batch, traceparent=None)
        except VlmError as e:
            # 측정은 부가 기능이라 보내지 못해도 확정 결과를 바꾸지 않는다(SPEC-013 R-6).
            logger.warning("stage timing not sent", extra={"error": type(e).__name__})
