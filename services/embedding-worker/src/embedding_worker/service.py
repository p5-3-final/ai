"""입력 이벤트 하나를 텍스트 벡터 파일과 `embedding.ready`로 바꾼다(SPEC-025)."""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import EmbedRequest, EmbedResponse
from vlm_contracts.events import (
    EmbeddingReady,
    Event,
    EventAnalyzed,
    SegmentAnnotated,
    TrackAnalyzed,
)
from vlm_contracts.vectors import TextVectorFile
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectStorage

from embedding_worker import ids, texts
from embedding_worker.files import vector_key
from embedding_worker.texts import Skip, TextItem
from embedding_worker.tracks import TrackLookup

_TASK_TYPE = "embed_text"
# R-9 단서: 기다려도 결과가 같은 오류. 재시도 없이 DLQ로 보낸다.
_PERMANENT_CODES = frozenset({"AI_MODEL_NOT_CONFIGURED", "AI_BUDGET_EXCEEDED"})
# R-9: 분당 한도는 잠시 뒤 풀리므로 4xx지만 다시 처리한다.
_RATE_LIMITED = "AI_RATE_LIMITED"
_INVALID_RESPONSE = "AI_INVALID_RESPONSE"

logger = logging.getLogger(__name__)


class EmbedClient(Protocol):
    """ai-gateway `/v1/embed` 호출. 운영은 `vlm_ai_client.client.AiGatewayClient`다."""

    async def embed(
        self, request: EmbedRequest, *, traceparent: str | None = None
    ) -> EmbedResponse: ...


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


class EmbeddingService:
    """메시지 하나에서 항목 하나를 만들어 임베딩하고, 파일을 저장한 뒤 `embedding.ready`를 낸다.

    문장이 없거나 조건에 맞지 않는 메시지는 호출·저장·발행 없이 건너뛴다(R-4). 저장·발행 오류는
    그대로 던져 워커가 재시도 토픽으로 보낸다. 다시 처리하면 같은 경로·같은 eventId다(R-12).
    """

    def __init__(
        self,
        client: EmbedClient,
        storage: ObjectStorage,
        publisher: EventPublisher,
        tracks: TrackLookup,
        *,
        max_chars: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._storage = storage
        self._publisher = publisher
        self._tracks = tracks
        self._max_chars = max_chars
        self._clock = clock

    async def handle_event(self, message: EventAnalyzed, ctx: MessageContext) -> None:
        """`event.analyzed` 경로 핸들러(R-1)."""
        await self._process(message, texts.event_item(message, self._max_chars), ctx)

    async def handle_track(self, message: TrackAnalyzed, ctx: MessageContext) -> None:
        """`track.analyzed` 경로 핸들러(R-2). 건너뛸 메시지는 번역표를 읽지 않는다."""
        skip = texts.track_skip(message)
        if skip is not None:
            await self._process(message, skip, ctx)
            return
        # 저장소 읽기는 블로킹이라 이벤트 루프 밖에서 한다.
        track = await asyncio.to_thread(
            self._tracks.find, message.source_id, message.job_id, message.track_id
        )
        await self._process(message, texts.track_item(message, track, self._max_chars), ctx)

    async def handle_annotation(self, message: SegmentAnnotated, ctx: MessageContext) -> None:
        """`segment.annotated` 경로 핸들러(R-3)."""
        await self._process(message, texts.annotation_item(message, self._max_chars), ctx)

    async def _process(self, message: Event, item: TextItem | Skip, ctx: MessageContext) -> None:
        if isinstance(item, Skip):
            # R-4, NFR-2: 이유 코드와 입력 eventId만 남긴다.
            logger.info(
                "embedding skipped",
                extra={
                    "inputEventId": str(message.event_id),
                    "eventType": message.event_type,
                    "reason": item.reason,
                },
            )
            return
        with ctx.timing.stage("embedding.call", unit="texts") as block:
            block.count = 1
            response = await self._embed(item, ctx)
        vector_file, ready = self._results(item, response)
        with ctx.timing.stage("embedding.store", unit="files") as block:
            block.count = 1
            # 파일을 먼저 저장한다. 소비자가 이벤트를 받았을 때 파일이 있어야 한다.
            await asyncio.to_thread(
                self._storage.put_bytes,
                ready.vector_key,
                vector_file.model_dump_json(by_alias=True).encode(),
                "application/json",
            )
        await asyncio.to_thread(self._publish, ready, ctx.traceparent)

    async def _embed(self, item: TextItem, ctx: MessageContext) -> EmbedResponse:
        """문장 하나를 임베딩한다. 503·연결 실패 재시도는 클라이언트가 한다(SPEC-021 R-10).

        Raises:
            AiGatewayError: 다시 처리하면 나아질 수 있는 실패(R-9). 워커가 재시도 토픽으로 보낸다.
            NonRetryableError: 모델 미설정·예산 초과(R-9 단서), 4xx·잘못된 응답(R-10).
        """
        request = EmbedRequest(task_type=_TASK_TYPE, texts=[item.text])
        try:
            response = await self._client.embed(request, traceparent=ctx.traceparent)
        except AiGatewayError as e:
            # NFR-2: 문장은 로그에 싣지 않는다.
            logger.warning(
                "embedding call failed",
                extra={
                    "itemKind": item.item_kind,
                    "itemId": str(item.item_id),
                    "code": e.code,
                    "status": e.status,
                },
            )
            if _is_permanent(e):
                raise NonRetryableError(f"ai-gateway rejected the call: {e.code}") from e
            raise
        # R-10: 문장 하나에 벡터 하나, 길이는 응답 차원과 같아야 한다.
        if len(response.vectors) != 1 or len(response.vectors[0]) != response.dimension:
            raise NonRetryableError(
                f"{_INVALID_RESPONSE}: expected one vector of length {response.dimension}"
            )
        return response

    def _results(
        self, item: TextItem, response: EmbedResponse
    ) -> tuple[TextVectorFile, EmbeddingReady]:
        key = vector_key(item.source_id, item.job_id, item.item_kind, item.item_id)
        try:
            vector_file = TextVectorFile(
                source_id=item.source_id,
                job_id=item.job_id,
                item_kind=item.item_kind,
                item_id=item.item_id,
                text=item.text,
                text_format=item.text_format,
                embedding_model=response.model_id,
                dimension=response.dimension,
                vector=response.vectors[0],
                metadata=item.metadata,
            )
            ready = EmbeddingReady(
                event_id=ids.ready_event_id(item.item_kind, item.item_id, response.model_id),
                occurred_at=self._clock(),
                source_id=item.source_id,
                owner_id=item.owner_id,
                job_id=item.job_id,
                item_kind=item.item_kind,
                item_id=item.item_id,
                vector_key=key,
                text_format=item.text_format,
                embedding_model=response.model_id,
                dimension=response.dimension,
            )
        except ValidationError as e:
            # 응답 모델 ID·차원이 계약 범위를 벗어났다(R-10). 다시 불러도 같다. 예외 메시지에
            # 문장·벡터가 들어가지 않도록 원인 예외의 내용은 싣지 않는다(NFR-2).
            raise NonRetryableError(
                f"{_INVALID_RESPONSE}: embedding response breaks the text vector contract"
            ) from e
        return vector_file, ready

    def _publish(self, event: EmbeddingReady, traceparent: str | None) -> None:
        self._publisher.publish(
            event.event_type,
            prepare_event(event, self._clock()),
            key=str(event.source_id),
            traceparent=traceparent,
        )


def _is_permanent(error: AiGatewayError) -> bool:
    """R-9·R-10 분류. 503·429 한도·연결 실패·시간 초과와 502·504·그 밖의 5xx는 다시 처리한다."""
    if error.code in _PERMANENT_CODES or error.code == _INVALID_RESPONSE:
        return True
    status = error.status
    return status is not None and 400 <= status < 500 and error.code != _RATE_LIMITED
