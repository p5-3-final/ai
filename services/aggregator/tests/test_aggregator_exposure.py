"""위험별 노출 지표(SPEC-027 R-16)."""

from agg_support import AISLE, DURATION_MS, EMPTY_INITIAL, HAZARD, JOB, person, risk_event, stay
from aggregator.exposure import exposure_of
from aggregator.state import run_state


def test_spec027_r16_zone_risk_counts_person_presences_overlapping_active_span() -> None:
    """[SPEC-027 R-16] 활성 30~60초 동안 같은 구역에 있던 사람 트랙 수와 겹친 시간 합."""
    [risk] = run_state(
        [risk_event(1, 30_000, detected=True), risk_event(2, 60_000, detected=False)],
        EMPTY_INITIAL,
        job_id=JOB,
    ).risks
    tracks = (person(10, 0, 100_000), person(11, 0, 100_000), person(12, 0, 100_000, "forklift"))
    presences = (
        stay(10, HAZARD, 20_000, 40_000),  # 10초 겹침
        stay(10, HAZARD, 50_000, 55_500),  # 5.5초 겹침, 같은 트랙
        stay(11, HAZARD, 60_000, 70_000),  # 경계만 닿음
        stay(12, HAZARD, 30_000, 60_000),  # 사람 아님
        stay(11, AISLE, 30_000, 60_000),  # 다른 구역
    )

    exposure = exposure_of(risk, DURATION_MS, tracks, presences, ("person",))

    assert (exposure.active_seconds, exposure.exposure_count, exposure.exposure_seconds) == (
        30.0,
        1,
        15.5,
    )


def test_spec027_r16_global_risk_uses_person_tracks_until_video_end() -> None:
    """[SPEC-027 R-16] global이면 영상 전체의 사람 트랙, 해소가 없으면 영상 끝까지다."""
    [risk] = run_state(
        [risk_event(1, 100_000, detected=True, zone=None)], EMPTY_INITIAL, job_id=JOB
    ).risks
    tracks = (person(10, 0, 110_000), person(11, 120_000, 130_001))

    exposure = exposure_of(risk, 125_001, tracks, (), ("person",))

    assert (exposure.active_seconds, exposure.exposure_count, exposure.exposure_seconds) == (
        25.001,
        2,
        15.001,
    )
