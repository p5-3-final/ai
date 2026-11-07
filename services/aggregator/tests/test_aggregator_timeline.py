"""타임라인(SPEC-027 R-17)."""

from agg_support import EMPTY_INITIAL, HAZARD, JOB, carried_initial, event, risk_event, uid
from aggregator.state import run_state
from aggregator.timeline import build_timeline


def test_spec027_r17_items_are_ordered_by_time_then_ref_id() -> None:
    """[SPEC-027 R-17] 대표 이벤트·위험 활성·해소를 (atMs, refId) 순으로 놓는다."""
    on = risk_event(2, 30_000, detected=True)
    off = risk_event(3, 60_000, detected=False)
    other = event(1, start=25_000, end=30_000, zone=HAZARD)
    run = run_state([on, off], EMPTY_INITIAL, job_id=JOB)

    timeline = build_timeline([other, on, off], [], run.risks)

    [risk] = run.risks
    assert [(t.at_ms, t.kind) for t in timeline] == [
        (25_000, "event"),
        (25_000, "event"),
        (30_000, "risk_activated"),
        (55_000, "event"),
        (60_000, "risk_resolved"),
    ]
    assert [t.ref_id for t in timeline[:2]] == [uid(1), uid(2)]
    assert timeline[2].ref_id == risk.risk_id
    assert (timeline[2].event_type, timeline[2].zone_id, timeline[2].severity) == (
        "ppe_missing",
        str(HAZARD),
        None,
    )


def test_spec027_r17_carried_risk_has_no_activation_item() -> None:
    """[SPEC-027 R-17] 이어 온 위험은 이 영상에서 활성된 것이 아니라 활성 항목이 없다."""
    off = risk_event(1, 70_000, detected=False)
    run = run_state([off], carried_initial(uid(900)), job_id=JOB)

    timeline = build_timeline([off], [], run.risks)

    assert [t.kind for t in timeline] == ["event", "risk_resolved"]
