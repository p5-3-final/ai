"""입력 이벤트에서 임베딩할 항목과 문장을 만든다(SPEC-025 R-1~R-5, R-7).

문장 규칙 버전(textFormat)은 코드 상수다. 규칙을 바꾸면 버전을 올린다. 검색 인덱스는 버전이 다른
항목을 재임베딩 대상으로 본다(계약 `vectors/v1/text-vector-file`의 textFormat 설명).
"""

from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.events import (
    EventAnalyzed,
    ItemKind,
    SegmentAnnotated,
    TrackAnalyzed,
    TrackAttribute,
)
from vlm_contracts.tracks import MappedTrack
from vlm_contracts.vectors import TextVectorMetadata


@dataclass(frozen=True, slots=True)
class TextItem:
    """임베딩할 항목 하나."""

    item_kind: ItemKind
    item_id: UUID
    source_id: UUID
    owner_id: UUID
    job_id: UUID
    text: str
    text_format: str
    metadata: TextVectorMetadata


@dataclass(frozen=True, slots=True)
class Skip:
    """임베딩하지 않는 메시지와 그 이유 코드(R-4)."""

    reason: str


EVENT_FORMAT = "event-v1"
TRACK_FORMAT = "track-v1"
ANNOTATION_FORMAT = "segment_annotation-v1"


def event_item(message: EventAnalyzed, max_chars: int) -> TextItem | Skip:
    """R-1: 설명문·위험 사유·조치 제안 중 값이 있는 것을 이 순서로 줄바꿈으로 잇는다."""
    analysis = message.analysis
    parts = [analysis.description, analysis.risk_reason, analysis.solution]
    text = "\n".join(p for p in parts if p is not None and _has_text(p))
    if not _has_text(text):
        return Skip("event_no_text")
    return TextItem(
        item_kind="event",
        item_id=message.event_id,
        source_id=message.source_id,
        owner_id=message.owner_id,
        job_id=message.job_id,
        text=_clip(text, max_chars),
        text_format=EVENT_FORMAT,
        # R-7: 이벤트 시각은 계약에 늘 있는 startMs·endMs(ms)다. evidenceStart·End(초)는
        # 판정하지 못하면 null이라 쓰지 않는다.
        metadata=_metadata(
            start_ms=message.start_ms,
            end_ms=message.end_ms,
            event_type=analysis.event_type,
            severity=analysis.severity,
            verification_status=analysis.verification_status,
        ),
    )


def track_skip(message: TrackAnalyzed) -> Skip | None:
    """R-2·R-4: 확정된 속성 설명이 있는 event·click 결과만 임베딩한다. 아니면 이유 코드."""
    checked = _confirmed_attributes(message)
    return checked if isinstance(checked, Skip) else None


def track_item(message: TrackAnalyzed, track: MappedTrack, max_chars: int) -> TextItem | Skip:
    """R-2: 트랙 속성 설명. 시각·클래스는 track-stitcher 번역표의 최종 트랙 값이다(R-7)."""
    checked = _confirmed_attributes(message)
    if isinstance(checked, Skip):
        return checked
    attributes, description = checked
    return TextItem(
        item_kind="track",
        item_id=message.track_id,
        source_id=message.source_id,
        owner_id=message.owner_id,
        job_id=message.job_id,
        text=_clip(description, max_chars),
        text_format=TRACK_FORMAT,
        metadata=_metadata(
            start_ms=track.start_ms,
            end_ms=track.end_ms,
            class_name=track.class_name,
            track_id=message.track_id,
            helmet=attributes.helmet,
            vest=attributes.vest,
            role=attributes.role,
        ),
    )


def annotation_item(message: SegmentAnnotated, max_chars: int) -> TextItem | Skip:
    """R-3: 정밀 검색 구간 주석 문장."""
    if not _has_text(message.text):
        return Skip("annotation_no_text")
    return TextItem(
        item_kind="segment_annotation",
        item_id=message.annotation_id,
        source_id=message.source_id,
        owner_id=message.owner_id,
        job_id=message.job_id,
        text=_clip(message.text, max_chars),
        text_format=ANNOTATION_FORMAT,
        metadata=_metadata(
            start_ms=message.start_ms, end_ms=message.end_ms, segment_id=message.segment_id
        ),
    )


def _confirmed_attributes(message: TrackAnalyzed) -> tuple[TrackAttribute, str] | Skip:
    if message.reason == "question":
        return Skip("track_question")
    attributes = message.attributes
    if message.outcome != "READY" or attributes is None:
        return Skip("track_failed")
    if attributes.verification_status != "confirmed":
        return Skip("track_not_confirmed")
    description = attributes.description
    if description is None or not _has_text(description):
        return Skip("track_no_description")
    return attributes, description


def _metadata(**values: object) -> TextVectorMetadata:
    # 계약 metadata는 null을 받지 않는다. 값이 없는 필터는 키를 뺀다(R-7).
    return TextVectorMetadata.model_validate({k: v for k, v in values.items() if v is not None})


def _has_text(value: str | None) -> bool:
    # 공백뿐인 문자열은 임베딩할 내용이 없다고 본다(R-4).
    return value is not None and value.strip() != ""


def _clip(text: str, max_chars: int) -> str:
    # R-5: 파이썬 문자열 길이(코드 포인트) 기준으로 앞에서부터 자른다.
    return text[:max_chars]
