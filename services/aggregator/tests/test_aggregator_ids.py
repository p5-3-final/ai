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


def test_spec028_r17_inventory_ids_are_deterministic_and_distinct() -> None:
    """[SPEC-028 R-17] 변화·재고 이벤트·객체 ID는 jobId와 객체로 정해지고 서로 겹치지 않는다."""
    change = ids.change_id(JOB, "moved", "a1", "b2")
    made = [
        change,
        ids.change_id(JOB, "removed", "a1", None),
        ids.change_id(JOB, "appeared", None, "b2"),
        ids.change_id(uid(99), "moved", "a1", "b2"),
        ids.inventory_event_id(JOB, change),
        ids.equipment_event_id(JOB, "a1"),
    ]

    assert change == ids.change_id(JOB, "moved", "a1", "b2")
    assert len(set(made)) == len(made)


def test_spec028_r17_object_id_fits_contract_and_differs_by_chunk() -> None:
    """[SPEC-028 R-17] 객체 ID는 계약 패턴(64자 이하)이고 같은 로컬 ID라도 청크가 다르면 다르다.

    같은 job의 chunkId(UUIDv7)는 앞 8자가 시각이라 겹칠 수 있어 앞 8자만으로는 나누지 못한다.
    """
    a = ids.object_id(uid(0x0192F0D6_0000_7000_8000_000000000001), "s1")
    b = ids.object_id(uid(0x0192F0D6_0000_7000_8000_000000000002), "s1")

    assert a != b
    assert a == ids.object_id(uid(0x0192F0D6_0000_7000_8000_000000000001), "s1")
    assert a.startswith("0192f0d6-")
    assert len(a) <= 64
