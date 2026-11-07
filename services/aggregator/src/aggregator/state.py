"""업로드 job 하나의 상태 계산(R-11~R-15). 전이는 `fold`가 하고 여기서는 입력을 고르고 묶는다."""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from vlm_contracts.aggregates import InitialState, StateSnapshot

from aggregator.fold import RiskRecord, SessionState, fold
from aggregator.model import InputEvent


@dataclass(frozen=True, slots=True)
class StateRun:
    """상태 계산 결과.

    Attributes:
        snapshots: 시간순 스냅샷.
        risks: 이 영상의 위험 전체(해소·활성·이어 온 위험). (firstSeenMs, riskId) 순이다.
        final: 영상 끝 상태.
    """

    snapshots: tuple[StateSnapshot, ...]
    risks: tuple[RiskRecord, ...]
    final: SessionState


def confirmed_in_order(representatives: Sequence[InputEvent]) -> list[InputEvent]:
    """병합 대표 중 확정(`confirmed`)만 이벤트 시각·eventId 순으로 고른다(R-11)."""
    confirmed = [e for e in representatives if e.analysis.verification_status == "confirmed"]
    return sorted(confirmed, key=lambda e: (e.at_ms, e.event_id))


def run_state(
    events: Sequence[InputEvent],
    initial: InitialState,
    *,
    job_id: UUID,
    source_id: UUID | None = None,
) -> StateRun:
    """초기 상태(R-15)에서 확정 이벤트 전체를 한 번 fold한다."""
    final, delta = fold(SessionState.from_initial(initial, source_id), events, job_id=job_id)
    risks = sorted(
        [*delta.resolved, *final.active_risks], key=lambda r: (r.first_seen_ms, r.risk_id)
    )
    return StateRun(delta.snapshots, tuple(risks), final)
