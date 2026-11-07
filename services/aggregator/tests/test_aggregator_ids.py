"""결정적 ID(SPEC-027 R-20, R-28)."""

from agg_support import HAZARD, JOB, uid
from aggregator import ids


def test_spec027_r20_ids_are_deterministic_and_distinct() -> None:
    """[SPEC-027 R-20] 같은 입력이면 같은 ID, 대상·입력이 다르면 다른 ID다."""
    made = [
        ids.merge_group_id(JOB, uid(1)),
        ids.rule_event_id(JOB, "dwell", uid(10), HAZARD, 60_000),
        ids.rule_event_id(JOB, "loiter", uid(10), HAZARD, 60_000),
        ids.snapshot_id(JOB, uid(1), str(HAZARD)),
        ids.risk_id(JOB, str(HAZARD), "ppe_missing", uid(1)),
        ids.source_completed_id(JOB, 1),
        ids.state_updated_id(JOB, 1),
    ]

    assert made[0] == ids.merge_group_id(JOB, uid(1))
    assert len(set(made)) == len(made)


def test_spec027_r28_source_completed_id_changes_with_revision() -> None:
    """[SPEC-027 R-28] revision을 올려 다시 내면 eventId도 바뀐다(소비자 중복 제거 기준)."""
    assert ids.source_completed_id(JOB, 1) != ids.source_completed_id(JOB, 2)
