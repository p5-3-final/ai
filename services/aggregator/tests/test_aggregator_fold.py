"""fold 하나가 업로드·실시간 창을 모두 처리한다(SPEC-027 R-30, ADR-0016)."""

from agg_support import EMPTY_INITIAL, JOB, analysis, carried_initial, event, risk_event, uid
from aggregator.fold import FoldDelta, SessionState, fold
from aggregator.model import InputEvent


def _events() -> list[InputEvent]:
    return [
        risk_event(1, 10_000, detected=True),
        event(2, start=15_000, end=25_000, result=analysis(after_state="a")),
        risk_event(3, 30_000, detected=True),
        risk_event(4, 45_000, detected=False),
        risk_event(5, 50_000, detected=True, zone=None),
        event(6, start=55_000, end=60_000, result=analysis(after_state="b")),
    ]


def test_spec027_r30_windowed_folds_equal_one_fold() -> None:
    """[SPEC-027 R-30] 창 단위로 이어 fold한 결과가 한 번 fold한 결과와 같다."""
    events = _events()
    start = SessionState.from_initial(carried_initial(uid(900)))

    once, delta = fold(start, events, job_id=JOB)
    state, deltas = start, []
    for window in (events[:2], events[2:4], events[4:]):
        state, part = fold(state, window, job_id=JOB)
        deltas.append(part)

    assert state == once
    assert [s for d in deltas for s in d.snapshots] == list(delta.snapshots)
    assert [r for d in deltas for r in d.resolved] == list(delta.resolved)
    assert [t for d in deltas for t in d.transitions] == list(delta.transitions)


def test_spec027_r30_state_round_trips_as_json() -> None:
    """[SPEC-027 R-30] 세션 상태와 delta는 Redis·S3에 JSON으로 저장해도 같다."""
    state, delta = fold(SessionState.from_initial(EMPTY_INITIAL), _events(), job_id=JOB)

    assert SessionState.model_validate_json(state.model_dump_json()) == state
    assert FoldDelta.model_validate_json(delta.model_dump_json()) == delta
