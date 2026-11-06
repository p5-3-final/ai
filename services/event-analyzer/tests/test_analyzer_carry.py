"""상태형 유형의 구간 규칙(SPEC-023 R-17, R-18 전제)."""

from uuid import UUID

from ea_support import detected
from event_analyzer.carry import plan_carry
from vlm_contracts.events import DetectableEventType

INTERVALS: dict[DetectableEventType, float] = {"ppe_missing": 120.0}


def _ids(plan: dict[UUID, UUID | None]) -> dict[int, int | None]:
    return {k.int: (v.int if v is not None else None) for k, v in plan.items()}


def test_spec023_r17_judges_first_and_every_interval_and_carries_between() -> None:
    starts = ((1, 0), (2, 30), (3, 120), (4, 121), (5, 200))
    candidates = [detected(i, start_s=s) for i, s in starts]

    plan = plan_carry(candidates, INTERVALS)

    # 0초 판정 → 30·120초 복사(간격 이하) → 121초 판정 → 200초 복사
    assert _ids(plan) == {0xD002: 0xD001, 0xD003: 0xD001, 0xD005: 0xD004}


def test_spec023_r17_event_types_without_interval_are_not_carried() -> None:
    falls = [detected(1, "fall", start_s=0), detected(2, "fall", start_s=30)]

    assert plan_carry(falls, INTERVALS) == {}


def test_spec023_r17_tracks_and_rule_candidates_are_separate() -> None:
    candidates = [
        detected(1, start_s=0, track="t1"),
        detected(2, start_s=30, track="t2"),
        detected(3, start_s=40, track=None),
        detected(4, start_s=50, track="t1", judgment="rule"),
    ]

    assert plan_carry(candidates, INTERVALS) == {}


def test_spec023_r17_same_input_gives_same_plan_regardless_of_order() -> None:
    candidates = [detected(i, start_s=10 * i) for i in range(1, 6)]

    assert plan_carry(candidates, INTERVALS) == plan_carry(candidates[::-1], INTERVALS)


def test_spec023_r18_prior_judgment_within_interval_carries_the_first_candidate() -> None:
    candidates = [detected(1, start_s=100), detected(2, start_s=150), detected(3, start_s=230)]

    plan = plan_carry(candidates, INTERVALS, {("t1", "ppe_missing"): 20_000})

    # 직전 창의 20초 판정에서 120초 안인 100초는 이어 받고, 150초는 판정, 230초는 그 복사
    assert _ids(plan) == {0xD001: None, 0xD003: 0xD002}


def test_spec023_r18_prior_not_before_first_candidate_is_ignored() -> None:
    # 다시 처리할 때 Redis에 이번 창의 판정이 이미 있어도 결과가 바뀌지 않는다.
    candidates = [detected(1, start_s=100)]

    assert plan_carry(candidates, INTERVALS, {("t1", "ppe_missing"): 100_000}) == {}
