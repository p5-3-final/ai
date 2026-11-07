"""`grounding.requested`의 구간마다 bbox 주석 클립을 만들어 `grounding.completed`로 알린다.

SPEC-026.

구간 하나의 순서(plan 4절 "처리 순서"): 재사용 확인 → 원본 확인 → probe·범위 검사 → 샘플 → 탐지
→ 렌더링 → 저장(클립 → 결과 파일) → 발행. 재사용 확인부터 렌더링까지를 구간 제한 시간으로 감싼다.
저장은 제한 시간 밖이다. 스레드에서 도는 업로드를 중간에 끊으면 결과 파일만 남을 수 있기 때문이다.
"""

import asyncio
import logging
import math
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError
from vlm_common.errors import VlmError
from vlm_contracts.events import (
    Event,
    GroundingCompleted,
    GroundingErrorCode,
    GroundingFrameCount,
    GroundingRequested,
    GroundingSegment,
)
from vlm_contracts.groundings import (
    GroundingBox,
    GroundingClip,
    GroundingDetection,
    GroundingResultFile,
    GroundingResultFrame,
)
from vlm_messaging.publisher import prepare_event
from vlm_messaging.worker import MessageContext
from vlm_storage.storage import ObjectStorage

from grounding_worker import ids, keys
from grounding_worker.detector import Detection, DetectorError, GroundingDetector, RgbFrame
from grounding_worker.media import MediaInfo, MediaReadError, MediaTool
from grounding_worker.profile import RenderProfile
from grounding_worker.render import (
    Window,
    box_windows,
    filter_script,
    label_files,
    target_color,
)
from grounding_worker.sampling import sample_times

# 서명 주소는 구간 제한 시간이 끝나기 전에 만료되면 안 된다. 렌더링 시작이 늦어질 여유를 더한다.
_PRESIGN_MARGIN_S = 60
# 결과 파일 계약의 프레임당 탐지 상한(maxItems)
_MAX_DETECTIONS = 100

logger = logging.getLogger(__name__)


class EventPublisher(Protocol):
    """이벤트 발행기. 운영은 `vlm_messaging.publisher.KafkaPublisher`다."""

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class GroundingConfig:
    """처리 설정. 기준 신뢰도·렌더링은 프로필, 나머지는 환경변수 값이다."""

    score_threshold: float
    render: RenderProfile
    sample_interval_ms: int = 1_000
    max_samples: int = 12
    item_timeout_s: float = 120
    # 구간마다 임시 폴더를 만드는 곳. None이면 시스템 임시 폴더다.
    work_dir: Path | None = None


class _SegmentFailedError(VlmError):
    """구간 하나를 FAILED로 끝낼 사유. 서비스 안에서만 쓴다."""

    def __init__(self, code: GroundingErrorCode) -> None:
        super().__init__(code)
        self.code: GroundingErrorCode = code


@dataclass(frozen=True, slots=True)
class _Made:
    """렌더링까지 끝난 구간. 저장 전 상태다."""

    clip_path: Path
    result: GroundingResultFile


class GroundingService:
    """요청 하나의 구간을 순서대로 처리하고 구간마다 `grounding.completed`를 한 건 발행한다."""

    def __init__(
        self,
        storage: ObjectStorage,
        media: MediaTool,
        detector: GroundingDetector,
        publisher: EventPublisher,
        config: GroundingConfig,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._media = media
        self._detector = detector
        self._publisher = publisher
        self._config = config
        self._clock = clock

    async def handle(self, message: GroundingRequested, ctx: MessageContext) -> None:
        """워커 경로 핸들러. 저장소·발행 오류는 그대로 던져 메시지 전체를 재시도한다(R-14).

        다시 처리하면 끝난 구간은 결과 파일로 재사용된다(R-8). 모든 구간을 발행한 뒤 돌아와야
        워커가 커밋한다(NFR-3).
        """
        for segment in message.segments:
            event = await self._segment(message, segment, ctx)
            # 발행은 블로킹이라 이벤트 루프 밖에서 한다.
            await asyncio.to_thread(self._publish, event, ctx.traceparent)

    async def _segment(
        self, message: GroundingRequested, segment: GroundingSegment, ctx: MessageContext
    ) -> GroundingCompleted:
        model = self._detector.version
        clip_key = keys.clip_key(message.source_id, segment.grounding_id, model)
        result_key = keys.result_key(message.source_id, segment.grounding_id, model)
        reused = await self._reuse(result_key)
        if reused is not None:
            return self._ready(message, segment, reused)
        with tempfile.TemporaryDirectory(prefix="grounding-", dir=self._config.work_dir) as tmp:
            try:
                async with asyncio.timeout(self._config.item_timeout_s) as scope:
                    made = await self._make(message, segment, clip_key, Path(tmp), ctx)
            except _SegmentFailedError as e:
                return self._failed(message, segment, e.code)
            except TimeoutError:
                # 저장소 연결 시간 초과 같은 다른 TimeoutError는 재시도해야 한다(R-14).
                if not scope.expired():
                    raise
                return self._failed(message, segment, "GROUNDING_TIMEOUT")
            await self._store(made, clip_key, result_key, ctx)
        return self._ready(message, segment, made.result)

    async def _reuse(self, result_key: str) -> GroundingResultFile | None:
        """R-8: 결과 파일이 있으면 그 내용. 결과 파일은 클립 다음에 쓰므로 있으면 클립도 있다."""
        if not await asyncio.to_thread(self._storage.exists, result_key):
            return None
        data = await asyncio.to_thread(self._storage.get_bytes, result_key)
        try:
            return GroundingResultFile.model_validate_json(data)
        except ValidationError:
            # 깨진 결과 파일로 READY를 낼 수는 없다. 다시 만들어 같은 경로에 덮어쓴다.
            logger.warning("stored grounding result is invalid", extra={"resultKey": result_key})
            return None

    async def _make(
        self,
        message: GroundingRequested,
        segment: GroundingSegment,
        clip_key: str,
        work: Path,
        ctx: MessageContext,
    ) -> _Made:
        if segment.end_ms <= segment.start_ms:
            raise _SegmentFailedError("GROUNDING_SEGMENT_OUT_OF_RANGE")
        url = await self._source_url(message.normalized_key)
        info = await self._probe(url)
        # C-10: ffprobe 길이는 마지막 프레임 시작이나 반올림 값이라 프레임 하나만큼 짧을 수 있다.
        if segment.end_ms > info.duration_ms + info.frame_interval_ms:
            raise _SegmentFailedError("GROUNDING_SEGMENT_OUT_OF_RANGE")
        times = [
            t
            for t in sample_times(
                segment.start_ms,
                segment.end_ms,
                interval_ms=self._config.sample_interval_ms,
                max_samples=self._config.max_samples,
            )
            if t < info.duration_ms
        ]
        if not times:
            raise _SegmentFailedError("GROUNDING_SEGMENT_OUT_OF_RANGE")

        with ctx.timing.stage("grounding.sample", unit="frames") as block:
            frames = [await self._frame(url, t, info) for t in times]
            block.count = len(frames)
        with ctx.timing.stage("grounding.detect", unit="frames") as block:
            found = [await self._detect(frame, message.targets) for frame in frames]
            block.count = len(found)
        with ctx.timing.stage("grounding.render", unit="frames") as block:
            clip_path = work / "clip.mp4"
            clip = await self._render(url, segment, info, times, found, work, clip_path)
            block.count = round(clip.duration_ms * clip.fps / 1000)

        result = GroundingResultFile(
            source_id=message.source_id,
            grounding_id=segment.grounding_id,
            segment_id=segment.segment_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            targets=message.targets,
            grounding_model=self._detector.version,
            # C-8: 기준 신뢰도는 결과 파일에만 둔다. 이벤트 계약에 자리가 없다.
            score_threshold=self._config.score_threshold,
            sample_fps=1000 / self._config.sample_interval_ms,
            colors={t: target_color(t) for t in message.targets},
            clip=GroundingClip(
                key=clip_key,
                fps=clip.fps,
                width=clip.width,
                height=clip.height,
                duration_ms=clip.duration_ms,
                audio=clip.has_audio,
            ),
            frames=[
                GroundingResultFrame(pts_ms=t, detections=[_detection(d) for d in dets])
                for t, dets in zip(times, found, strict=True)
            ],
        )
        return _Made(clip_path, result)

    async def _source_url(self, key: str) -> str:
        # R-11: 원본이 없으면 FAILED다. 저장소 연결 오류는 그대로 던져 재시도한다(R-14).
        if not await asyncio.to_thread(self._storage.exists, key):
            raise _SegmentFailedError("GROUNDING_SOURCE_UNREADABLE")
        expires_s = math.ceil(self._config.item_timeout_s) + _PRESIGN_MARGIN_S
        return await asyncio.to_thread(self._storage.presigned_get_url, key, expires_s=expires_s)

    async def _probe(self, url: str) -> MediaInfo:
        try:
            return await self._media.probe(url)
        except MediaReadError as e:
            raise _SegmentFailedError("GROUNDING_SOURCE_UNREADABLE") from e

    async def _frame(self, url: str, pts_ms: int, info: MediaInfo) -> RgbFrame:
        try:
            return await self._media.extract_frame(url, pts_ms, info)
        except MediaReadError as e:
            raise _SegmentFailedError("GROUNDING_SOURCE_UNREADABLE") from e

    async def _detect(self, frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        # 프레임 하나씩 스레드로 넘겨, 제한 시간(R-13)이 프레임 사이에서 끊기게 한다.
        try:
            detections = await asyncio.to_thread(self._detector.detect, frame, targets)
        except DetectorError as e:
            raise _SegmentFailedError("GROUNDING_DETECTOR_FAILED") from e
        if any(not _valid(d, targets) for d in detections):
            # 대상어 밖이거나 0~1 밖의 결과는 탐지기 오류다. 결과 파일 계약을 깨지 않는다.
            raise _SegmentFailedError("GROUNDING_DETECTOR_FAILED")
        kept = [d for d in detections if d.confidence >= self._config.score_threshold]  # R-3
        return sorted(kept, key=lambda d: -d.confidence)[:_MAX_DETECTIONS]

    async def _render(
        self,
        url: str,
        segment: GroundingSegment,
        info: MediaInfo,
        times: list[int],
        found: list[list[Detection]],
        work: Path,
        clip_path: Path,
    ) -> MediaInfo:
        script = await asyncio.to_thread(
            self._write_script, found, box_windows(times, segment.start_ms), info, work
        )
        try:
            await self._media.render_clip(
                url,
                start_ms=segment.start_ms,
                duration_ms=segment.end_ms - segment.start_ms,
                script=script,
                dst=clip_path,
                info=info,
                profile=self._config.render,
            )
            clip = await self._media.probe(str(clip_path))
        except MediaReadError as e:
            raise _SegmentFailedError("GROUNDING_SOURCE_UNREADABLE") from e
        if clip.video_codec != "h264":
            raise _SegmentFailedError("GROUNDING_SOURCE_UNREADABLE")
        return clip

    def _write_script(
        self, found: list[list[Detection]], windows: list[Window], info: MediaInfo, work: Path
    ) -> Path:
        targets = sorted({d.target for dets in found for d in dets})
        script = work / "filter.txt"
        script.write_text(
            filter_script(
                found,
                windows,
                width=info.width,
                height=info.height,
                labels=label_files(targets, work),
                profile=self._config.render,
            ),
            encoding="utf-8",
        )
        return script

    async def _store(
        self, made: _Made, clip_key: str, result_key: str, ctx: MessageContext
    ) -> None:
        # R-6: 클립 → 결과 파일 순서. 결과 파일이 있으면 클립도 있다는 것을 재사용(R-8)이 믿는다.
        with ctx.timing.stage("grounding.store", unit="objects") as block:
            await asyncio.to_thread(self._storage.put_file, clip_key, made.clip_path, "video/mp4")
            body = made.result.model_dump_json(by_alias=True).encode()
            await asyncio.to_thread(self._storage.put_bytes, result_key, body, "application/json")
            block.count = 2

    def _ready(
        self, message: GroundingRequested, segment: GroundingSegment, result: GroundingResultFile
    ) -> GroundingCompleted:
        frames = [
            # R-7: 탐지가 없는 대상어도 0으로 담는다.
            GroundingFrameCount(
                pts_ms=f.pts_ms,
                counts={t: sum(d.target == t for d in f.detections) for t in message.targets},
            )
            for f in result.frames
        ]
        return self._completed(
            message,
            segment,
            "READY",
            clip_key=result.clip.key,
            result_key=keys.result_key(
                message.source_id, segment.grounding_id, result.grounding_model
            ),
            frames=frames,
            model=result.grounding_model,
        )

    def _failed(
        self, message: GroundingRequested, segment: GroundingSegment, code: GroundingErrorCode
    ) -> GroundingCompleted:
        # 대상어·질의는 사용자 입력이라 로그에 싣지 않는다.
        logger.warning(
            "grounding failed",
            extra={
                "requestId": str(message.request_id),
                "groundingId": str(segment.grounding_id),
                "code": code,
            },
        )
        return self._completed(message, segment, "FAILED", error_code=code)

    def _completed(
        self,
        message: GroundingRequested,
        segment: GroundingSegment,
        outcome: Literal["READY", "FAILED"],
        *,
        clip_key: str | None = None,
        result_key: str | None = None,
        frames: list[GroundingFrameCount] | None = None,
        error_code: GroundingErrorCode | None = None,
        model: str | None = None,
    ) -> GroundingCompleted:
        grounding_model = model or self._detector.version
        return GroundingCompleted(
            event_id=ids.completed_event_id(
                segment.grounding_id, grounding_model, message.request_id, outcome
            ),
            occurred_at=self._clock(),
            source_id=message.source_id,
            owner_id=message.owner_id,
            request_id=message.request_id,
            job_id=message.job_id,
            grounding_id=segment.grounding_id,
            segment_id=segment.segment_id,
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            targets=message.targets,
            grounding_model=grounding_model,
            outcome=outcome,
            clip_key=clip_key,
            result_key=result_key,
            frames=frames,
            error_code=error_code,
        )

    def _publish(self, event: GroundingCompleted, traceparent: str | None) -> None:
        self._publisher.publish(
            event.event_type,
            prepare_event(event, self._clock()),
            key=str(event.source_id),
            traceparent=traceparent,
        )


def _valid(detection: Detection, targets: Sequence[str]) -> bool:
    box = detection.box
    values = (box.x, box.y, box.w, box.h, detection.confidence)
    return detection.target in targets and all(0 <= v <= 1 for v in values)


def _detection(detection: Detection) -> GroundingDetection:
    box = detection.box
    return GroundingDetection(
        target=detection.target,
        box=GroundingBox(x=box.x, y=box.y, w=box.w, h=box.h),
        confidence=detection.confidence,
    )
