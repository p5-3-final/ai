"""위험별 노출 지표(R-16). 위험 상태별 노출은 예측 모델의 정답으로도 쓴다(v4 ⑥)."""

from collections.abc import Collection, Sequence

from vlm_contracts.aggregates import GLOBAL_ZONE, Exposure

from aggregator.fold import RiskRecord
from aggregator.model import Presence, TrackSpan


def seconds(ms: int) -> float:
    """ms를 초로 바꾼다. 소수 셋째 자리에서 끊어 직렬화가 늘 같게 한다(R-21)."""
    return round(ms / 1000, 3)


def overlap(start: int, end: int, other_start: int, other_end: int) -> int:
    return max(0, min(end, other_end) - max(start, other_start))


def exposure_of(
    risk: RiskRecord,
    end_ms: int,
    tracks: Sequence[TrackSpan],
    presences: Sequence[Presence],
    person_classes: Collection[str],
    *,
    start_ms: int | None = None,
) -> Exposure:
    """활성 구간(활성 시작 ~ 해소, 없으면 `end_ms`) 동안의 노출.

    구역 위험은 그 구역 머문 구간 중 사람 트랙, global은 사람 트랙 관측 구간 전체와 겹친 길이로
    센다.
    겹친 길이가 0인(경계만 닿은) 트랙은 세지 않는다.

    Args:
        start_ms: 셈을 시작할 시각. 실시간 창 증분은 창 시작이다. 없으면 활성 시작이다.
    """
    begin = risk.first_seen_ms if start_ms is None else max(risk.first_seen_ms, start_ms)
    finish = max(begin, risk.active_until(end_ms))
    people = {t.track_id: t for t in tracks if t.class_name in person_classes}
    spans: list[tuple[object, int, int]]
    if risk.zone_id == GLOBAL_ZONE:
        spans = [(t.track_id, t.start_ms, t.end_ms) for t in people.values()]
    else:
        spans = [
            (p.track_id, p.enter_ms, p.exit_ms)
            for p in presences
            if str(p.zone_id) == risk.zone_id and p.track_id in people
        ]
    total = 0
    exposed: set[object] = set()
    for track_id, s, e in spans:
        shared = overlap(begin, finish, s, e)
        if shared > 0:
            total += shared
            exposed.add(track_id)
    return Exposure(
        active_seconds=seconds(finish - begin),
        exposure_count=len(exposed),
        exposure_seconds=seconds(total),
    )
