"""계산 전체와 결정적 직렬화(SPEC-027 R-17, R-20, R-21)."""

import json

from agg_support import (
    CHUNK_A,
    CHUNK_B,
    HAZARD,
    RESULT_ID,
    analysis,
    carried_initial,
    contract_errors,
    event,
    inputs,
    person,
    profile,
    risk_event,
    stay,
    uid,
)
from aggregator.compute import compute
from aggregator.model import AggregateInput
from aggregator.result import build_result, empty_summary, encode_result


def _scenario() -> AggregateInput:
    return inputs(
        event(1, start=50_000, end=55_000, chunk=CHUNK_A, track=uid(10), score=0.9),
        event(2, start=58_000, end=62_000, chunk=CHUNK_B, track=uid(10), score=0.2),
        risk_event(3, 100_000, detected=True),
        risk_event(4, 200_000, detected=False),
        event(5, start=10_000, result=analysis(status="rejected")),
        event(6, start=300_000, zone=HAZARD, result=analysis(after_state="정리됨")),
        tracks=(person(10, 0, 400_000), person(11, 0, 400_000)),
        presences=(stay(10, HAZARD, 90_000, 170_000), stay(11, HAZARD, 150_000, 160_000)),
        initial=carried_initial(uid(900)),
    )


def _shuffled(source: AggregateInput, seed: int) -> AggregateInput:
    # 결정적으로 섞는다: 뒤집은 뒤 seed만큼 돌린다.
    def mix[T](items: tuple[T, ...]) -> tuple[T, ...]:
        flipped = list(reversed(items)) if seed % 2 else list(items)
        shift = seed % max(1, len(flipped))
        return tuple(flipped[shift:] + flipped[:shift])

    return AggregateInput(
        source_id=source.source_id,
        owner_id=source.owner_id,
        job_id=source.job_id,
        normalization_id=source.normalization_id,
        duration_ms=source.duration_ms,
        session=source.session,
        initial_state=source.initial_state,
        events=mix(source.events),
        tracks=mix(source.tracks),
        presences=mix(source.presences),
        zones=mix(source.zones),
    )


def _encoded(source: AggregateInput) -> bytes:
    loaded = profile()
    computed = compute(source, loaded)
    return encode_result(build_result(source, computed, empty_summary("empty"), loaded.version))


def test_spec027_r21_same_input_in_any_order_gives_identical_bytes() -> None:
    """[SPEC-027 R-21] 입력 순서를 섞어도 요약을 뺀 결과 파일이 바이트 단위로 같다."""
    first = _encoded(_scenario())

    assert all(_encoded(_shuffled(_scenario(), seed)) == first for seed in range(5))


def test_spec027_r20_result_file_passes_contract_and_has_expected_parts() -> None:
    """[SPEC-027 R-20] 결과 파일이 계약 스키마를 통과하고, 병합·규칙·스냅샷·위험이 담긴다."""
    body = json.loads(_encoded(_scenario()))

    assert contract_errors(RESULT_ID, body) == []
    assert [g["keptEventId"] for g in body["mergeGroups"]] == [str(uid(1))]
    assert [r["rule"] for r in body["ruleEvents"]] == ["dwell"]
    assert body["ruleEvents"][0]["severity"] == "high"
    carried = next(r for r in body["risks"] if r["riskId"] == str(uid(900)))
    assert carried["carriedOver"] is True
    # 대표가 아닌 이벤트(2)는 타임라인에 없다(R-7, R-17).
    assert str(uid(2)) not in {t["refId"] for t in body["timeline"]}


def test_spec027_us1_no_confirmed_events_gives_empty_lists() -> None:
    """[SPEC-027 US-1] 확정 이벤트가 없으면 스냅샷·위험이 빈 목록이다."""
    loaded = profile()
    source = inputs(event(1, start=0, result=analysis(status="unverified")))

    computed = compute(source, loaded)

    assert (computed.snapshots, computed.risks, len(computed.confirmed)) == ((), (), 0)
