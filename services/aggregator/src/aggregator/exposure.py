"""위험별 노출 지표(R-16). 위험 상태별 노출은 예측 모델의 정답으로도 쓴다(v4 ⑥)."""

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from vlm_contracts.aggregates import GLOBAL_ZONE, Exposure

from aggregator.fold import RiskRecord
from aggregator.model import Presence, TrackSpan


def seconds(ms: int) -> float:
    """ms를 초로 바꾼다. 소수 셋째 자리에서 끊어 직렬화가 늘 같게 한다(R-21)."""
    return round(ms / 1000, 3)


def overlap(start: int, end: int, other_start: int, other_end: int) -> int:
    return max(0, min(end, other_end) - max(start, other_start))


Span = tuple[object, int, int]


@dataclass(frozen=True, slots=True)
class ExposureSpans:
    """노출 계산에 쓰는 사람 트랙 구간. job 하나에 한 번 만들어 위험마다 다시 쓴다(NFR-1).

    Attributes:
        people: 사람 트랙 관측 구간(global 위험).
        by_zone: 구역 id(문자열)별 사람 트랙의 머문 구간(구역 위험).
    """

    people: tuple[Span, ...]
    by_zone: Mapping[str, tuple[Span, ...]]

    @classmethod
    def of(
        cls,
        tracks: Sequence[TrackSpan],
        presences: Sequence[Presence],
        person_classes: Collection[str],
    ) -> "ExposureSpans":
        people = {t.track_id: t for t in tracks if t.class_name in person_classes}
        grouped: dict[str, list[Span]] = defaultdict(list)
        for p in presences:
            if p.track_id in people:
                grouped[str(p.zone_id)].append((p.track_id, p.enter_ms, p.exit_ms))
        return cls(
            people=tuple((t.track_id, t.start_ms, t.end_ms) for t in people.values()),
            by_zone={zone: tuple(spans) for zone, spans in grouped.items()},
        )


def exposure_of(
    risk: RiskRecord,
    end_ms: int,
    tracks: Sequence[TrackSpan],
    presences: Sequence[Presence],
    person_classes: Collection[str],
    *,
    start_ms: int | None = None,
) -> Exposure:
    """위험 하나의 노출. 위험이 여럿이면 `ExposureSpans`를 한 번 만들어 `exposure_in`을 쓴다."""
    spans = ExposureSpans.of(tracks, presences, person_classes)
    return exposure_in(risk, end_ms, spans, start_ms=start_ms)


def exposure_in(
    risk: RiskRecord, end_ms: int, spans: ExposureSpans, *, start_ms: int | None = None
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
    candidates = (
        spans.people if risk.zone_id == GLOBAL_ZONE else spans.by_zone.get(risk.zone_id, ())
    )
    total = 0
    exposed: set[object] = set()
    for track_id, s, e in candidates:
        shared = overlap(begin, finish, s, e)
        if shared > 0:
            total += shared
            exposed.add(track_id)
    return Exposure(
        active_seconds=seconds(finish - begin),
        exposure_count=len(exposed),
        exposure_seconds=seconds(total),
    )
