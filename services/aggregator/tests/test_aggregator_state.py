"""상태 스냅샷·위험 전이·세션 이어받기(SPEC-027 R-11~R-15)."""

from agg_support import (
    EMPTY_INITIAL,
    HAZARD,
    JOB,
    analysis,
    carried_initial,
    event,
    risk_event,
    uid,
)
from aggregator.state import confirmed_in_order, run_state


def test_spec027_r11_only_confirmed_in_event_time_order_and_global_zone() -> None:
    """[SPEC-027 R-11] 확정만, 이벤트 시각(evidenceEnd ms, 없으면 endMs)·eventId 순.

    구역이 없으면 global이다.
    """
    late_by_evidence = event(
        3, start=0, end=1_000, result=analysis(after_state="b", evidence_end=50.0)
    )
    early = event(2, start=10_000, end=20_000, result=analysis(after_state="a"))
    tie = event(1, start=10_000, end=20_000, result=analysis(after_state="c"))
    rejected = event(4, start=0, end=1_000, result=analysis(status="rejected", after_state="x"))

    ordered = confirmed_in_order([late_by_evidence, early, tie, rejected])
    run = run_state(ordered, EMPTY_INITIAL, job_id=JOB)

    assert [e.event_id for e in ordered] == [uid(1), uid(2), uid(3)]
    assert [s.timestamp_ms for s in run.snapshots] == [20_000, 20_000, 50_000]
    assert {s.zone_id for s in run.snapshots} == {"global"}


def test_spec027_r12_after_state_makes_snapshot_with_previous_state() -> None:
    """[SPEC-027 R-12] 이전 상태(처음이면 초기 상태, 없으면 null)에서 afterState로 바뀐다."""
    first = event(1, start=0, end=10_000, zone=HAZARD, result=analysis(after_state="작업 중"))
    second = event(2, start=20_000, end=30_000, zone=HAZARD, result=analysis(after_state="정리"))

    empty = run_state([first, second], EMPTY_INITIAL, job_id=JOB)
    carried = run_state([first], carried_initial(uid(900)), job_id=JOB)

    assert [(s.previous_state, s.current_state) for s in empty.snapshots] == [
        (None, "작업 중"),
        ("작업 중", "정리"),
    ]
    assert carried.snapshots[0].previous_state == "방호벽 설치됨"
    assert empty.final.zone_states == {str(HAZARD): "정리"}


def test_spec027_r13_risk_is_created_then_updated_in_same_zone_and_type() -> None:
    """[SPEC-027 R-13] 같은 구역·유형의 활성 위험이 있으면 lastSeen과 관련 이벤트를 갱신한다."""
    run = run_state(
        [risk_event(1, 30_000, detected=True), risk_event(2, 50_000, detected=True)],
        EMPTY_INITIAL,
        job_id=JOB,
    )

    [risk] = run.risks
    assert (risk.first_seen_ms, risk.last_seen_ms, risk.resolved_at_ms) == (30_000, 50_000, None)
    assert risk.related_event_ids == (uid(1), uid(2))
    assert risk.reason == "안전모 미착용"


def test_spec027_r13_other_zone_or_type_is_a_separate_risk() -> None:
    run = run_state(
        [
            risk_event(1, 30_000, detected=True),
            risk_event(2, 40_000, detected=True, zone=None),
            risk_event(3, 50_000, detected=True, risk_type="fall_risk"),
        ],
        EMPTY_INITIAL,
        job_id=JOB,
    )

    assert sorted((r.zone_id, r.risk_type) for r in run.risks) == [
        (str(HAZARD), "fall_risk"),
        (str(HAZARD), "ppe_missing"),
        ("global", "ppe_missing"),
    ]


def test_spec027_r14_false_resolves_and_reactivation_gets_a_new_risk_id() -> None:
    """[SPEC-027 R-14] riskDetected=false면 해소, 활성 위험이 없으면 변화 없음.

    해소 뒤 다시 나오면 새 riskId다.
    """
    run = run_state(
        [
            risk_event(1, 10_000, detected=False),
            risk_event(2, 30_000, detected=True),
            risk_event(3, 60_000, detected=False),
            risk_event(4, 90_000, detected=True),
        ],
        EMPTY_INITIAL,
        job_id=JOB,
    )

    first, again = run.risks
    assert (first.status, first.resolved_at_ms) == ("resolved", 60_000)
    assert first.related_event_ids == (uid(2), uid(3))
    assert (again.status, again.first_seen_ms) == ("active", 90_000)
    assert again.risk_id != first.risk_id


def test_spec027_r15_carried_risk_keeps_id_starts_at_zero_and_can_resolve() -> None:
    """[SPEC-027 R-15] 이어 온 위험은 원래 riskId, firstSeenMs 0, carriedOver true로 시작한다."""
    carried = run_state([], carried_initial(uid(900)), job_id=JOB)
    resolved = run_state(
        [risk_event(1, 70_000, detected=False)], carried_initial(uid(900)), job_id=JOB
    )

    [kept] = carried.risks
    [ended] = resolved.risks
    assert (kept.risk_id, kept.first_seen_ms, kept.carried_over, kept.status) == (
        uid(900),
        0,
        True,
        "active",
    )
    assert (ended.risk_id, ended.status, ended.resolved_at_ms) == (uid(900), "resolved", 70_000)
