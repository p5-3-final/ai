"""쿨다운 병합 규칙(SPEC-023 R-3, US-3)."""

from ea_support import detected
from event_analyzer.merge import Kept, merge_candidates


def _kept_ids(kept: list[Kept]) -> list[tuple[int, list[int]]]:
    return [(k.candidate.candidate_id.int, [i.int for i in k.merged_ids]) for k in kept]


def test_spec023_r3_us3_repeats_within_cooldown_keep_highest_score() -> None:
    first = detected(1, start_s=0, score=0.6)
    best = detected(2, start_s=3, score=0.9)
    third = detected(3, start_s=6, score=0.7)

    kept = merge_candidates([first, best, third], cooldown_s=10)

    assert _kept_ids(kept) == [(0xD002, [0xD001, 0xD003])]


def test_spec023_r3_gap_equal_to_cooldown_merges_and_longer_gap_splits() -> None:
    # 앞 후보 끝(1초)과 다음 시작 간격이 정확히 10초면 합치고, 10초를 넘으면 나눈다.
    at_limit = [detected(1, start_s=0), detected(2, start_s=11)]
    over = [detected(1, start_s=0), detected(2, start_s=11.001)]

    assert len(merge_candidates(at_limit, cooldown_s=10)) == 1
    assert len(merge_candidates(over, cooldown_s=10)) == 2


def test_spec023_r3_gap_is_measured_from_the_previous_candidate() -> None:
    chain = [detected(1, start_s=0), detected(2, start_s=8), detected(3, start_s=16)]

    assert len(merge_candidates(chain, cooldown_s=10)) == 1


def test_spec023_r3_us3_candidates_without_track_are_not_merged() -> None:
    fires = [
        detected(1, "fire_or_smoke", start_s=0, track=None),
        detected(2, "fire_or_smoke", start_s=1, track=None),
    ]

    assert _kept_ids(merge_candidates(fires, cooldown_s=10)) == [(0xD001, []), (0xD002, [])]


def test_spec023_r3_different_track_or_type_is_not_merged() -> None:
    candidates = [
        detected(1, start_s=0, track="t1"),
        detected(2, start_s=1, track="t2"),
        detected(3, "fall", start_s=2, track="t1"),
    ]

    assert len(merge_candidates(candidates, cooldown_s=10)) == 3


def test_spec023_r3_ties_keep_earlier_start_then_smaller_id() -> None:
    earlier = detected(5, start_s=0, score=0.8)
    later = detected(1, start_s=2, score=0.8)
    same_start = [detected(7, start_s=0, score=0.8), detected(6, start_s=0, score=0.8)]

    assert _kept_ids(merge_candidates([later, earlier], cooldown_s=10)) == [(0xD005, [0xD001])]
    assert _kept_ids(merge_candidates(same_start, cooldown_s=10)) == [(0xD006, [0xD007])]


def test_spec023_r3_kept_candidates_stay_in_input_order() -> None:
    candidates = [
        detected(1, "fire_or_smoke", start_s=50, track=None),
        detected(2, start_s=0, score=0.5),
        detected(3, start_s=2, score=0.9),
        detected(4, "fall", start_s=1, track="t9"),
    ]

    kept = merge_candidates(candidates, cooldown_s=10)

    assert [k.candidate.candidate_id.int for k in kept] == [0xD001, 0xD003, 0xD004]


def test_spec023_r11_no_candidates_gives_nothing() -> None:
    assert merge_candidates([], cooldown_s=10) == []
