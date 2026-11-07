"""위험구역 체류·배회 규칙 이벤트(SPEC-027 R-8~R-10)."""

from agg_support import AISLE, EMPTY_INITIAL, HAZARD, JOB, ZONES, person, profile, risk_event, stay
from aggregator.rules import rule_events
from aggregator.state import run_state

TRACKS = (person(10, 0, 600_000), person(11, 0, 600_000, "forklift"))


def test_spec027_r8_dwell_at_or_above_threshold_in_hazard_zone() -> None:
    """[SPEC-027 R-8] hazard 구역에 사람 트랙이 기준(60초) 이상 머문 구간마다 dwell이다."""
    events = rule_events(
        (
            stay(10, HAZARD, 0, 70_000),
            stay(10, HAZARD, 100_000, 160_000),  # 정확히 60초
            stay(10, HAZARD, 200_000, 259_999),  # 기준 미만
            stay(11, HAZARD, 0, 100_000),  # 사람 아님
            stay(10, AISLE, 0, 100_000),  # hazard 아님
        ),
        TRACKS,
        ZONES,
        (),
        profile(),
        job_id=JOB,
    )

    # 세 구간은 300초 안의 세 번 진입이라 loiter도 하나 나온다(R-9). 여기서는 dwell만 본다.
    assert [(e.start_ms, e.end_ms) for e in events if e.rule == "dwell"] == [
        (0, 70_000),
        (100_000, 160_000),
    ]


def test_spec027_r9_loiter_counts_entries_within_window_then_restarts() -> None:
    """[SPEC-027 R-9] 300초 안에 3번 들어오면 loiter 하나, 마지막 진입 뒤부터 다시 센다."""
    entries = [0, 100_000, 200_000, 250_000, 260_000, 700_000]
    presences = tuple(stay(10, HAZARD, t, t + 10_000) for t in entries)

    events = rule_events(presences, TRACKS, ZONES, (), profile(), job_id=JOB)

    loiters = [(e.start_ms, e.end_ms) for e in events if e.rule == "loiter"]
    # 0·100·200초로 하나, 다음 창은 250초부터라 250·260·700초는 창(300초)을 넘는다.
    assert loiters == [(0, 210_000)]


def test_spec027_r10_rule_event_fields_and_severity_by_active_risk_overlap() -> None:
    """[SPEC-027 R-10] confirmed·rule·템플릿 설명, 같은 구역 활성 위험과 겹치면 high."""
    risks = run_state(
        [risk_event(1, 30_000, detected=True), risk_event(2, 90_000, detected=False)],
        EMPTY_INITIAL,
        job_id=JOB,
    ).risks

    events = rule_events(
        (stay(10, HAZARD, 0, 70_000), stay(10, HAZARD, 100_000, 170_000)),
        TRACKS,
        ZONES,
        risks,
        profile(),
        job_id=JOB,
    )

    first, second = events
    assert (first.severity, second.severity) == ("high", "medium")
    assert (first.verification_status, first.judgment_method) == ("confirmed", "rule")
    assert first.description == "작업자가 'A동 크레인 하부'에 60초 이상 머물렀습니다."
    assert first.event_type == "hazard_zone_access"
