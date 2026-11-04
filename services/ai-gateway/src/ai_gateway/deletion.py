"""영상 삭제 전파: `video.deleted`를 받아 그 영상의 모델 원문을 지운다(SPEC-009 R-20, R-21)."""

import logging
import threading
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import create_async_engine
from vlm_common.health import HealthState
from vlm_contracts.events import VideoDeleted
from vlm_messaging.publisher import KafkaPublisher
from vlm_messaging.timing import KafkaTimingSink
from vlm_messaging.worker import KafkaWorker, MessageContext, WorkerConfig, route

from ai_gateway.call_log import CallLog

logger = logging.getLogger(__name__)


class DeletionHandler:
    """원문에는 영상 내용 묘사가 담겨 영상과 함께 지운다. 사용량 집계용 나머지 기록은 남긴다."""

    def __init__(self, call_log: CallLog) -> None:
        self._call_log = call_log

    async def handle(self, event: VideoDeleted, ctx: MessageContext) -> None:
        # 이미 지운 행은 건드리지 않으므로 같은 이벤트를 다시 받아도 결과가 같다(R-21).
        cleared = await self._call_log.clear_raw_output(event.source_id)
        logger.info(
            "raw outputs cleared",
            extra={"source_id": str(event.source_id), "cleared": cleared, "attempt": ctx.attempt},
        )


@dataclass(frozen=True)
class RunningWorker:
    """백그라운드 스레드에서 도는 `video.deleted` 소비자."""

    worker: KafkaWorker
    thread: threading.Thread

    def stop(self, timeout_s: float = 15.0) -> None:
        """처리 중인 메시지는 커밋하지 않고 멈춘다. 다른 Pod가 다시 처리한다."""
        self.worker.stop()
        self.thread.join(timeout=timeout_s)


def start_deletion_worker(
    bootstrap: str,
    group_id: str,
    database_url: str,
    health: HealthState,
    *,
    publish_timing: bool = False,
) -> RunningWorker:
    """`video.deleted` 소비자를 데몬 스레드로 띄운다.

    HTTP 서버와 같은 프로세스에서 돌아 배포 단위를 늘리지 않는다(plan 5절). 워커는 자기 이벤트
    루프에서 핸들러를 돌리므로, HTTP 쪽과 연결 풀을 나누려고 DB 엔진을 따로 만든다.

    Args:
        bootstrap: Kafka 부트스트랩 주소.
        group_id: 소비 그룹. 재시도·DLQ 토픽 이름에도 쓴다.
        database_url: ai_db 접속 URL.
        health: 구독을 시작하면 준비 상태로 표시된다(R-23).
        publish_timing: 처리 시간 측정 묶음을 `stage.timing`에 발행할지. 끄면 JSON 로그로만 남긴다
            (SPEC-013 R-5).
    """
    engine = create_async_engine(database_url)
    handler = DeletionHandler(CallLog(engine))
    publisher = KafkaPublisher(bootstrap=bootstrap, client_id=group_id)
    worker = KafkaWorker(
        # 단계 ID(`ai-gateway.handle` 등)는 소비 그룹 설정과 상관없이 서비스 이름으로 남긴다.
        WorkerConfig(bootstrap=bootstrap, group_id=group_id, service="ai-gateway"),
        [route("video.deleted", VideoDeleted, handler.handle)],
        publisher,
        health=health,
        timing=KafkaTimingSink(publisher) if publish_timing else None,
    )
    thread = threading.Thread(target=worker.run, name="video-deleted-consumer", daemon=True)
    thread.start()
    return RunningWorker(worker, thread)
