"""영상 삭제 전파: `video.deleted`를 받아 그 영상의 모델 원문을 지운다(SPEC-009 R-20, R-21)."""

import logging

from vlm_contracts.events import VideoDeleted
from vlm_messaging.worker import MessageContext

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
