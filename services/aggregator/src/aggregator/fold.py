"""상태 전이 fold(R-11~R-15, ADR-0016).

`fold(state, events) → (state', delta)` 한 함수가 업로드 완료(R-1)·실시간 창(R-30)·늦은 벡터
이벤트(R-27)의 상태 전이를 모두 맡는다. 순수 함수라 같은 상태와 이벤트면 같은 결과다(R-21).
`SessionState`는 JSON으로 Redis·S3에 저장하므로 이벤트 본문을 두지 않는다. SPEC-028은 재고를 이
상태에 더한다.
"""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from vlm_contracts.aggregates import GLOBAL_ZONE, InitialState, StateSnapshot

from aggregator import ids
from aggregator.model import InputEvent


class _Stored(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, frozen=True, extra="forbid"
    )


class RiskRecord(_Stored):
    """위험 하나의 현재 값. 활성 위험은 `SessionState`에, 해소된 위험은 `FoldDelta`에 나온다."""

    risk_id: UUID
    zone_id: str
    risk_type: str
    reason: str
    first_seen_ms: int
    last_seen_ms: int
    resolved_at_ms: int | None = None
    related_event_ids: tuple[UUID, ...] = ()
    carried_over: bool = False

    @property
    def status(self) -> Literal["active", "resolved"]:
        return "active" if self.resolved_at_ms is None else "resolved"

    def active_until(self, end_ms: int) -> int:
        """활성 구간의 끝. 해소되지 않았으면 `end_ms`(영상·창 끝)다."""
        return self.resolved_at_ms if self.resolved_at_ms is not None else end_ms


class SessionState(_Stored):
    """세션(영상·실시간 세션)의 현재 상태.

    Attributes:
        zone_states: 구역 ID(또는 global) → 현재 상태 문장.
        active_risks: 활성 위험. (firstSeenMs, riskId) 순이다.
        last_sequence: 처리한 마지막 창 순번(실시간). 업로드는 0이다.
        last_window_end_ms: 처리한 마지막 창 끝 시각(실시간).
        source_id: 이 상태의 영상(세션).
        from_source_id: 직전 영상(세션)에서 이어받았으면 그 영상(R-5, 실시간 회전).
    """

    zone_states: dict[str, str] = Field(default_factory=dict)
    active_risks: tuple[RiskRecord, ...] = ()
    last_sequence: int = 0
    last_window_end_ms: int = 0
    source_id: UUID | None = None
    from_source_id: UUID | None = None

    @classmethod
    def from_initial(cls, initial: InitialState, source_id: UUID | None = None) -> "SessionState":
        """직전 영상의 마지막 상태로 시작한다.

        이어 온 위험은 원래 riskId·firstSeenMs 0이다(R-15).
        """
        return cls(
            zone_states={z.zone_id: z.state for z in initial.zone_states},
            active_risks=tuple(
                RiskRecord(
                    risk_id=r.risk_id,
                    zone_id=r.zone_id,
                    risk_type=r.risk_type,
                    reason=r.reason,
                    first_seen_ms=0,
                    last_seen_ms=0,
                    carried_over=True,
                )
                for r in sorted(initial.active_risks, key=lambda r: r.risk_id)
            ),
            source_id=source_id,
        )

    def carried_into(self, source_id: UUID) -> "SessionState":
        """다른 영상(실시간 회전)으로 넘긴 상태.

        활성 위험은 이어 온 위험이 되고 창 순번은 처음부터다.
        """
        return SessionState(
            zone_states=dict(self.zone_states),
            active_risks=tuple(
                r.model_copy(update={"first_seen_ms": 0, "last_seen_ms": 0, "carried_over": True})
                for r in self.active_risks
            ),
            source_id=source_id,
            from_source_id=self.source_id,
        )


class Transition(_Stored):
    """위험 전이 하나. `risk`는 전이 뒤 값이다."""

    kind: Literal["activated", "updated", "resolved"]
    at_ms: int
    risk: RiskRecord


class FoldDelta(_Stored):
    """fold 한 번에서 바뀐 것. 실시간이면 `state.updated` 본문이 된다(R-30)."""

    snapshots: tuple[StateSnapshot, ...] = ()
    transitions: tuple[Transition, ...] = ()
    resolved: tuple[RiskRecord, ...] = ()


def zone_of(event: InputEvent) -> str:
    """상태 계산의 구역. 구역이 없는 이벤트는 global이다(R-11)."""
    return str(event.zone_id) if event.zone_id is not None else GLOBAL_ZONE


def fold(
    state: SessionState, events: Sequence[InputEvent], *, job_id: UUID
) -> tuple[SessionState, FoldDelta]:
    """확정 이벤트를 이벤트 시각·eventId 순으로 훑어 상태를 바꾼다(R-11~R-14).

    Args:
        state: 시작 상태.
        events: 상태 계산에 넣을 확정 대표 이벤트. 순서는 상관없다(안에서 정렬한다).
        job_id: 결정적 ID(스냅샷·위험)에 쓴다.
    """
    zone_states = dict(state.zone_states)
    active = {(r.zone_id, r.risk_type): r for r in state.active_risks}
    snapshots: list[StateSnapshot] = []
    transitions: list[Transition] = []
    resolved: list[RiskRecord] = []
    for e in sorted(events, key=lambda e: (e.at_ms, e.event_id)):
        zone, at, a = zone_of(e), e.at_ms, e.analysis
        if a.after_state is not None:
            snapshots.append(
                StateSnapshot(
                    snapshot_id=ids.snapshot_id(job_id, e.event_id, zone),
                    zone_id=zone,
                    timestamp_ms=at,
                    previous_state=zone_states.get(zone),
                    current_state=a.after_state,
                    related_event_id=e.event_id,
                )
            )
            zone_states[zone] = a.after_state
        if a.risk_type is None:
            continue
        key = (zone, a.risk_type)
        current = active.get(key)
        if a.risk_detected is True and a.risk_reason is not None:
            if current is None:
                updated = RiskRecord(
                    risk_id=ids.risk_id(job_id, zone, a.risk_type, e.event_id),
                    zone_id=zone,
                    risk_type=a.risk_type,
                    reason=a.risk_reason,
                    first_seen_ms=at,
                    last_seen_ms=at,
                    related_event_ids=(e.event_id,),
                )
                transitions.append(Transition(kind="activated", at_ms=at, risk=updated))
            else:
                updated = _seen(current, at, e.event_id)
                transitions.append(Transition(kind="updated", at_ms=at, risk=updated))
            active[key] = updated
        elif a.risk_detected is False and current is not None:
            # 활성 위험이 없으면 바꾸지 않는다(R-14). 해소 근거를 위험에서 찾게 관련 이벤트에
            # 더한다.
            ended = _seen(current, current.last_seen_ms, e.event_id).model_copy(
                update={"resolved_at_ms": at}
            )
            transitions.append(Transition(kind="resolved", at_ms=at, risk=ended))
            resolved.append(ended)
            del active[key]
    next_state = state.model_copy(
        update={
            "zone_states": zone_states,
            "active_risks": tuple(
                sorted(active.values(), key=lambda r: (r.first_seen_ms, r.risk_id))
            ),
        }
    )
    delta = FoldDelta(
        snapshots=tuple(snapshots), transitions=tuple(transitions), resolved=tuple(resolved)
    )
    return next_state, delta


def _seen(risk: RiskRecord, at: int, event_id: UUID) -> RiskRecord:
    related = risk.related_event_ids
    if event_id not in related:
        related = (*related, event_id)
    return risk.model_copy(update={"last_seen_ms": at, "related_event_ids": related})
