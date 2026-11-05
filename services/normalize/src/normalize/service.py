"""영상 하나를 정규화하는 처리 순서(SPEC-007 R-1~R-14)."""

import asyncio
import json
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel
from vlm_contracts.events import (
    Event,
    RejectReason,
    VideoNormalized,
    VideoNormalizing,
    VideoUploaded,
)
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.storage import ObjectNotFoundError, ObjectStorage

from normalize import ids
from normalize.decision import Limits, Reject, decide
from normalize.ffmpeg import DecodeError, MediaTool
from normalize.probe import MediaReadError, ProbeResult

logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
}


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class NormalizeConfig:
    """정규화 기준과 작업 폴더.

    Attributes:
        limits: 길이 하한·상한과 재인코딩 fps 상한.
        hls_segment_s: HLS 조각 목표 길이(초).
        work_dir: 원본·결과를 잠시 두는 폴더. 영상마다 하위 폴더를 만들고 끝나면 지운다.
        normalization_version: 정규화 방식의 버전. 바꾸면 같은 영상도 새 결과로 저장된다.
    """

    limits: Limits
    hls_segment_s: float
    work_dir: Path
    normalization_version: str


@dataclass(frozen=True, slots=True)
class _Output:
    """저장을 마친 정규화 사본의 정보."""

    probe: ProbeResult
    transcoded: bool


class ResultSnapshot(BaseModel):
    """최초 결과와 원 입력을 함께 보존해 같은 ID의 변경 입력도 구별한다."""

    request: VideoUploaded
    definition: str
    event: VideoNormalized


class NormalizeService:
    """`video.uploaded` 하나를 받아 정규화 결과를 저장하고 발행한다.

    원본 품질 때문에 처리할 수 없으면 REJECTED를 발행한다. 저장소·Kafka 장애처럼 원본과 무관한
    실패는 예외를 그대로 던져 워커가 다시 시도하게 한다(SPEC-007 R-13).
    """

    def __init__(
        self,
        storage: ObjectStorage,
        media: MediaTool,
        publisher: EventPublisher,
        config: NormalizeConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._media = media
        self._publisher = publisher
        self._config = config
        self._clock = clock

    async def handle(self, event: VideoUploaded, ctx: MessageContext) -> None:
        """워커 경로 핸들러. ffmpeg와 저장소 호출이 블로킹이라 스레드에서 처리한다."""
        await asyncio.to_thread(self._process, event, ctx.traceparent, ctx.timing)

    def _process(
        self, uploaded: VideoUploaded, traceparent: str | None, timing: StageRecorder
    ) -> None:
        source = uploaded.source_id
        normalization = ids.normalization_id(uploaded.event_id, self._config.normalization_version)
        # NFR-4: 영상 ID로만 식별하고 소유자·파일 정보는 남기지 않는다.
        log = {"sourceId": str(source), "normalizationId": str(normalization)}
        self._publish(
            VideoNormalizing(
                event_id=ids.normalizing_event_id(source),
                occurred_at=self._clock(),
                source_id=source,
                owner_id=uploaded.owner_id,
            ),
            traceparent,
        )
        snapshot_key = f"sources/{source}/normalizations/{normalization}/result-event.json"
        definition = json.dumps(
            {
                "version": self._config.normalization_version,
                "hls": self._config.hls_segment_s,
                "min": self._config.limits.min_duration_s,
                "max": self._config.limits.max_duration_s,
                "fps": self._config.limits.max_fps,
            },
            sort_keys=True,
        )
        try:
            cached = ResultSnapshot.model_validate_json(self._storage.get_bytes(snapshot_key))
        except ObjectNotFoundError:
            cached = None
        if cached is not None:
            self._check_snapshot(cached, uploaded, definition)
            self._publish(cached.event, traceparent)
            return
        # NFR-5: 성공·거절·예외 모두에서 작업 파일을 지우도록 임시 폴더 안에서만 처리한다.
        with tempfile.TemporaryDirectory(dir=self._config.work_dir) as tmp:
            work = Path(tmp)
            original = work / "original"
            try:
                self._storage.get_file(uploaded.storage_key, original)
            except ObjectNotFoundError:
                # 이미 삭제된 영상이다.
                # 먼저 보낸 video.normalizing은 media-service가 무시한다(R-18).
                logger.info("original not found, skipping", extra=log)
                return
            outcome = self._normalize(source, normalization, work, original, timing)
        if isinstance(outcome, _Output):
            result = self._ready(uploaded, normalization, outcome)
            logger.info("video normalized", extra={**log, "transcoded": outcome.transcoded})
        else:
            result = self._rejected(uploaded, normalization, outcome)
            logger.info("video rejected", extra={**log, "rejectReason": outcome})

        snapshot = ResultSnapshot(request=uploaded, definition=definition, event=result)
        self._storage.put_bytes_if_absent(
            snapshot_key, snapshot.model_dump_json().encode(), "application/json"
        )
        saved = ResultSnapshot.model_validate_json(self._storage.get_bytes(snapshot_key))
        self._check_snapshot(saved, uploaded, definition)
        if saved.event.model_dump(exclude={"occurred_at"}) != result.model_dump(
            exclude={"occurred_at"}
        ):
            raise NonRetryableError("normalization result conflicts with immutable snapshot")
        self._publish(saved.event, traceparent)

    @staticmethod
    def _check_snapshot(snapshot: ResultSnapshot, request: VideoUploaded, definition: str) -> None:
        if snapshot.request != request or snapshot.definition != definition:
            raise NonRetryableError("normalization input conflicts with immutable snapshot")

    def _normalize(
        self,
        source: UUID,
        normalization: UUID,
        work: Path,
        original: Path,
        timing: StageRecorder,
    ) -> _Output | RejectReason:
        """정규화 사본과 HLS를 저장하고 사본의 정보를 돌려준다. 거절이면 사유를 돌려준다.

        단계마다 걸린 시간을 남긴다(SPEC-013 R-9). 실패한 단계도 `error`로 남는다.
        """
        try:
            with timing.stage("normalize.probe"):
                probe = self._media.probe(original)
        except MediaReadError:
            return "UNREADABLE"
        decision = decide(probe, self._config.limits)
        if isinstance(decision, Reject):
            return decision.reason
        output = work / "video.mp4"
        try:
            with timing.stage("normalize.transcode"):
                self._media.normalize(original, output, decision, probe.audio_codec)
            with timing.stage("normalize.hls"):
                segments = self._media.make_hls(output, work / "hls", self._config.hls_segment_s)
            with timing.stage("normalize.probe"):
                normalized = self._media.probe(output)
        except (DecodeError, MediaReadError):
            return "DECODE_FAILED"
        with timing.stage("normalize.store", unit="files") as store:
            self._storage.put_file(
                ids.normalized_key(source, normalization), output, _CONTENT_TYPES[".mp4"]
            )
            # 재생 목록을 마지막에 올려, 목록이 보이면 조각도 모두 있게 한다.
            for path in sorted(segments, key=lambda p: p.suffix == ".m3u8"):
                key = f"{ids.hls_prefix(source, normalization)}{path.name}"
                self._storage.put_file(
                    key, path, _CONTENT_TYPES.get(path.suffix, "application/octet-stream")
                )
            store.count = 1 + len(segments)
        return _Output(normalized, decision.transcode)

    def _ready(
        self, uploaded: VideoUploaded, normalization: UUID, output: _Output
    ) -> VideoNormalized:
        source = uploaded.source_id
        if output.probe.duration_sec is None:
            # 사본 길이를 못 읽으면 pipeline이 계획을 세울 수 없다. 거절하지 않고 재시도한다.
            raise ValueError("normalized copy has no duration")
        millis = ids.duration_ms(output.probe.duration_sec)
        return VideoNormalized(
            event_id=ids.normalized_event_id(normalization),
            occurred_at=self._clock(),
            source_id=source,
            owner_id=uploaded.owner_id,
            normalization_id=normalization,
            normalization_version=self._config.normalization_version,
            result="READY",
            reject_reason=None,
            normalized_key=ids.normalized_key(source, normalization),
            hls_playlist_key=ids.hls_playlist_key(source, normalization),
            duration_ms=millis,
            duration_sec=millis / 1000,
            fps=output.probe.avg_fps,
            width=output.probe.width,
            height=output.probe.height,
            transcoded=output.transcoded,
        )

    def _rejected(
        self, uploaded: VideoUploaded, normalization: UUID, reason: RejectReason
    ) -> VideoNormalized:
        return VideoNormalized(
            event_id=ids.normalized_event_id(normalization),
            occurred_at=self._clock(),
            source_id=uploaded.source_id,
            owner_id=uploaded.owner_id,
            normalization_id=normalization,
            normalization_version=self._config.normalization_version,
            result="REJECTED",
            reject_reason=reason,
            normalized_key=None,
            hls_playlist_key=None,
            duration_ms=None,
            duration_sec=None,
            fps=None,
            width=None,
            height=None,
            transcoded=None,
        )

    def _publish(self, event: Event, traceparent: str | None) -> None:
        self._publisher.publish(event.event_type, event, traceparent=traceparent)
