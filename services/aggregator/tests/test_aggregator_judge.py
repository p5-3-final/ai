"""재고 변화·장비 방치 판정(SPEC-028 R-7~R-10, R-12, R-17)."""

import json
from uuid import UUID

from agg_support import (
    AISLE,
    CHUNK_A,
    CHUNK_B,
    HAZARD,
    JOB,
    PREVIOUS_SOURCE,
    SOURCE,
    TRACEPARENT,
    ZONES,
    FakeGateway,
    analysis,
    profile,
    state_compare_output,
    uid,
)
from aggregator import ids
from aggregator.equipment import abandoned_equipment
from aggregator.inventory import Comparison, compare
from aggregator.judge import InventoryJudge, Judged, LocalBudget
from aggregator.profile import AggregateProfile
from inv_support import chunk_inventory, previous_object, static
from vlm_ai_client.client import AiGatewayError
from vlm_storage.memory import InMemoryStorage

FAR = (0.7, 0.1, 0.2, 0.15)
GENERAL = uid(0xF4)


def _profile(**changes: object) -> AggregateProfile:
    return profile().profile.model_copy(update=changes)


def _moved() -> Comparison:
    return compare(
        (
            chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, zone=AISLE, last=41_000)),
            chunk_inventory(
                CHUNK_B, 60_000, 120_000, static("s2", CHUNK_B, box=FAR, zone=AISLE, first=70_000)
            ),
        ),
        (),
        job_id=JOB,
        profile=_profile(),
    )


async def _judge(
    comparison: Comparison,
    gateway: FakeGateway,
    *,
    loaded: AggregateProfile | None = None,
    storage: InMemoryStorage | None = None,
    abandoned_chains: bool = False,
    limit: int | None = None,
) -> Judged:
    p = loaded or _profile()
    abandoned = (
        abandoned_equipment(comparison.chains, (), (), p, job_id=JOB) if abandoned_chains else ()
    )
    return await InventoryJudge(gateway, storage or InMemoryStorage(), p).judge(
        comparison,
        abandoned,
        source_id=SOURCE,
        job_id=JOB,
        zones=ZONES,
        budget=LocalBudget(p.judge_call_limit if limit is None else limit),
        traceparent=TRACEPARENT,
    )


async def test_spec028_r7_moved_is_judged_with_two_frames_and_variables() -> None:
    """[SPEC-028 R-7] 직전 객체 대표 프레임과 이후 객체 대표 프레임, 기대 클래스·구역으로 부른다."""
    gateway = FakeGateway()
    comparison = _moved()

    await _judge(comparison, gateway)

    [(request, trace)] = gateway.requests
    [change] = comparison.changes
    assert (request.task_type, request.source_id, trace) == ("state_compare", SOURCE, TRACEPARENT)
    assert [i.storage_key for i in request.images] == [change.before_frame, change.after_frame]
    assert request.variables == {
        "eventType": "object_moved",
        "changeKind": "moved",
        "expectedClasses": ["safety_barrier"],
        "zoneName": "통로 1",
        "beforeSec": 41.0,
        "afterSec": 70.0,
    }


async def test_spec028_r7_appeared_is_recorded_without_a_call() -> None:
    """[SPEC-028 R-7] appeared는 부르지 않고 eventId·상태 null로 기록한다."""
    gateway = FakeGateway()
    before = previous_object("p1", source=PREVIOUS_SOURCE, cls="ladder")
    storage = InMemoryStorage()
    storage.put_bytes(before.frame_key, b"jpeg", "image/jpeg")
    comparison = compare(
        (chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, first=5_000)),),
        (before,),
        job_id=JOB,
        profile=_profile(move_window_ms=0),
    )

    judged = await _judge(comparison, gateway, storage=storage)

    assert gateway.task_types() == ["state_compare"]  # 직전 객체 removed만 판정한다
    appeared = [c for c in judged.changes if c.kind == "appeared"]
    assert [(c.event_id, c.call_id, c.verification_status) for c in appeared] == [
        (None, None, None)
    ]


async def test_spec028_r7_previous_video_frame_is_copied_into_this_job() -> None:
    """[SPEC-028 R-7, D-17 A] 직전 영상 프레임은 이 job 폴더로 복사해 보낸다.

    복사 위치는 `aggregate/frames/blurred/`다.
    """
    storage = InMemoryStorage()
    before = previous_object("p1", source=PREVIOUS_SOURCE, zone=HAZARD)
    storage.put_bytes(before.frame_key, b"previous-jpeg", "image/jpeg")
    gateway = FakeGateway()
    comparison = compare(
        (chunk_inventory(CHUNK_A, 0, 60_000),),
        (before,),
        job_id=JOB,
        profile=_profile(),
    )

    judged = await _judge(comparison, gateway, storage=storage)

    [(request, _)] = gateway.requests
    copied = request.images[0].storage_key
    assert copied.startswith(f"sources/{SOURCE}/jobs/{JOB}/aggregate/frames/blurred/")
    assert storage.get_bytes(copied) == b"previous-jpeg"
    assert request.variables["changeKind"] == "not_restored"
    assert request.variables["beforeSec"] is None
    # 결과 근거는 계약의 블러 사본 경로 규칙을 따르는 원래 경로다(계약 차이로 보고).
    [event] = judged.events
    assert event.evidence_frame_keys[0] == before.frame_key
    assert (event.event_type, event.start_ms, event.end_ms) == ("barrier_not_restored", 0, 0)


async def test_spec028_r8_calls_stop_at_the_cap_in_before_time_order() -> None:
    """[SPEC-028 R-8] 직전 시각 순으로 판정하고, 상한에 이르면 남은 후보는 null로 둔다."""
    gateway = FakeGateway()
    comparison = compare(
        (
            chunk_inventory(
                CHUNK_A,
                0,
                60_000,
                static("a", CHUNK_A, last=30_000),
                static("b", CHUNK_A, box=FAR, last=10_000),
            ),
            chunk_inventory(CHUNK_B, 60_000, 120_000),
        ),
        (),
        job_id=JOB,
        profile=_profile(),
    )

    judged = await _judge(comparison, gateway, limit=1)

    [(request, _)] = gateway.requests
    assert request.variables["beforeSec"] == 10.0
    by_object = {c.before_object_id: c for c in judged.changes}
    first = by_object[ids.object_id(CHUNK_A, "b")]
    skipped = by_object[ids.object_id(CHUNK_A, "a")]
    assert first.event_id == ids.inventory_event_id(JOB, first.change_id)
    assert first.verification_status == "confirmed"
    assert (skipped.event_id, skipped.call_id, skipped.verification_status) == (None, None, None)
    assert judged.calls == 1


async def test_spec028_r9_event_type_per_kind_and_analysis_from_state_compare() -> None:
    """[SPEC-028 R-9] moved는 object_moved 재고 이벤트이고 분석은 state_compare 결과다."""
    gateway = FakeGateway()

    judged = await _judge(_moved(), gateway)

    [event] = judged.events
    [change] = judged.changes
    assert event.event_id == change.event_id == ids.inventory_event_id(JOB, change.change_id)
    assert (event.event_type, event.judgment_method) == ("object_moved", "inventory+state_compare")
    assert (event.start_ms, event.end_ms, event.zone_id) == (41_000, 70_000, AISLE)
    assert event.analysis.verification_status == "confirmed"
    assert event.call_id == change.call_id is not None


async def test_spec028_r9_removed_is_object_removed() -> None:
    gateway = FakeGateway()
    gateway.task_outputs["state_compare"] = state_compare_output(event_type="object_removed")
    comparison = compare(
        (
            chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, last=41_000)),
            chunk_inventory(CHUNK_B, 60_000, 120_000),
        ),
        (),
        job_id=JOB,
        profile=_profile(),
    )

    judged = await _judge(comparison, gateway)

    assert [e.event_type for e in judged.events] == ["object_removed"]
    assert gateway.requests[0][0].variables["eventType"] == "object_removed"


async def test_spec028_r10_gateway_error_is_unknown_analysis() -> None:
    """[SPEC-028 R-10] 호출이 재시도 뒤에도 오류면 unknown 분석으로 기록한다."""
    gateway = FakeGateway()
    gateway.task_errors["state_compare"] = AiGatewayError(
        "AI_PROVIDER_UNAVAILABLE", "x", status=503, retryable=True
    )

    judged = await _judge(_moved(), gateway)

    [event] = judged.events
    [change] = judged.changes
    assert event.analysis.verification_status == "unknown"
    assert event.analysis.event_type == "object_moved"
    assert (event.call_id, change.verification_status) == (None, "unknown")


async def test_spec028_r10_missing_previous_frame_is_unknown_without_a_call() -> None:
    """[SPEC-028 R-10] 직전 영상 프레임이 지워져 복사할 수 없으면 부르지 않고 unknown이다."""
    gateway = FakeGateway()
    comparison = compare(
        (chunk_inventory(CHUNK_A, 0, 60_000),),
        (previous_object("p1", source=PREVIOUS_SOURCE),),
        job_id=JOB,
        profile=_profile(),
    )

    judged = await _judge(comparison, gateway)

    assert gateway.requests == []
    assert [e.analysis.verification_status for e in judged.events] == ["unknown"]


async def test_spec028_r10_invalid_output_is_unknown_analysis() -> None:
    gateway = FakeGateway()
    gateway.task_outputs["state_compare"] = {"analysis": {"eventType": "fall"}, "changes": []}

    judged = await _judge(_moved(), gateway)

    assert judged.events[0].analysis.verification_status == "unknown"


def _forklift(zone: UUID) -> Comparison:
    return compare(
        (
            chunk_inventory(
                CHUNK_A, 0, 900_000, static("f1", CHUNK_A, cls="forklift", zone=zone, last=720_000)
            ),
        ),
        (),
        job_id=JOB,
        profile=_profile(),
    )


async def test_spec028_r12_aisle_equipment_is_judged_with_event_judge() -> None:
    """[SPEC-028 R-12] 통로·위험구역 장비 방치는 대표 프레임으로 event_judge를 불러 rule+vlm이다."""
    gateway = FakeGateway(
        judge_output=json.loads(analysis("equipment_abandoned").model_dump_json(by_alias=True))
    )
    comparison = _forklift(AISLE)

    judged = await _judge(comparison, gateway, abandoned_chains=True)

    [(request, _)] = gateway.requests
    assert request.task_type == "event_judge"
    assert [i.storage_key for i in request.images] == [comparison.chains[0].sightings[-1].frame_key]
    assert request.variables == {
        "eventType": "equipment_abandoned",
        "startSec": 0.0,
        "endSec": 720.0,
        "zoneName": "통로 1",
        "evidence": {
            "className": "forklift",
            "stationarySeconds": 720.0,
            "rule": "equipment_abandoned",
        },
    }
    [event] = judged.events
    assert (event.event_type, event.judgment_method) == ("equipment_abandoned", "rule+vlm")
    assert event.event_id == ids.equipment_event_id(JOB, comparison.chains[0].object_id)


async def test_spec028_r12_general_zone_equipment_is_template_rule_event() -> None:
    """[SPEC-028 R-12] 그 밖 구역은 부르지 않고 템플릿 설명의 rule·confirmed 이벤트다."""
    gateway = FakeGateway()

    judged = await _judge(_forklift(GENERAL), gateway, abandoned_chains=True)

    assert gateway.requests == []
    [event] = judged.events
    assert (event.judgment_method, event.analysis.verification_status) == ("rule", "confirmed")
    assert event.analysis.description is not None
    assert "10분" in event.analysis.description
    assert event.call_id is None


async def test_spec028_r12_equipment_counts_against_cap_and_over_cap_is_unverified() -> None:
    """[SPEC-028 R-8·R-12] 장비 방치 판정도 상한에 센다. 넘으면 unverified rule+vlm으로 둔다."""
    gateway = FakeGateway()

    judged = await _judge(_forklift(HAZARD), gateway, abandoned_chains=True, limit=0)

    assert gateway.requests == []
    [event] = judged.events
    assert (event.judgment_method, event.analysis.verification_status) == (
        "rule+vlm",
        "unverified",
    )


async def test_spec028_r17_same_input_makes_same_candidates_and_ids() -> None:
    """[SPEC-028 R-17] 같은 입력이면 판정 결과를 뺀 후보·ID·이미지 경로가 같다."""
    first, second = FakeGateway(), FakeGateway()
    a = await _judge(_moved(), first)
    b = await _judge(_moved(), second)

    assert [(c.change_id, c.event_id) for c in a.changes] == [
        (c.change_id, c.event_id) for c in b.changes
    ]
    assert [r.images for r, _ in first.requests] == [r.images for r, _ in second.requests]
