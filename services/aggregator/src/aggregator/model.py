"""계산 입력의 내부 값 객체. 읽기 API 응답과 Kafka 메시지를 이 모양으로 바꾼 뒤 계산한다."""

from dataclasses import dataclass, field
from uuid import UUID

from vlm_contracts.aggregates import InitialState
from vlm_contracts.events import EventType, SessionRef, VLMAnalysis


@dataclass(frozen=True, slots=True)
class InputEvent:
    """판정 이벤트 하나(`listJobEvents` 항목 또는 `event.analyzed`)."""

    event_id: UUID
    chunk_id: UUID | None
    event_type: EventType
    start_ms: int
    end_ms: int
    track_id: UUID | None
    zone_id: UUID | None
    zone_name: str | None
    judgment_method: str
    score: float | None
    analysis: VLMAnalysis

    @property
    def at_ms(self) -> int:
        """상태 계산의 이벤트 시각(R-11). 근거 끝(초)이 있으면 그것, 없으면 endMs다."""
        if self.analysis.evidence_end is not None:
            return round(self.analysis.evidence_end * 1000)
        return self.end_ms


@dataclass(frozen=True, slots=True)
class TrackSpan:
    """최종 트랙 하나의 관측 구간."""

    track_id: UUID
    class_name: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class Presence:
    """트랙이 구역 안에 머문 구간 하나."""

    track_id: UUID
    zone_id: UUID
    enter_ms: int
    exit_ms: int


@dataclass(frozen=True, slots=True)
class ZoneInfo:
    """구역 정의. `kind`가 `hazard`면 체류·배회 규칙 대상이다."""

    zone_id: UUID
    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class AggregateInput:
    """job 하나의 계산 입력. 요약을 뺀 결과는 이 값과 프로필로만 정해진다(R-21)."""

    source_id: UUID
    owner_id: UUID
    job_id: UUID
    normalization_id: UUID
    duration_ms: int
    session: SessionRef | None
    initial_state: InitialState
    events: tuple[InputEvent, ...] = ()
    tracks: tuple[TrackSpan, ...] = ()
    presences: tuple[Presence, ...] = ()
    zones: tuple[ZoneInfo, ...] = field(default=())
