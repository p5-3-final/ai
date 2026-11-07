"""업로드 job의 재고 단계(SPEC-028 R-2, R-6, R-9, R-13~R-15, R-18)."""

import json
from typing import cast

import pytest
from agg_support import (
    AISLE,
    CHUNK_A,
    CHUNK_B,
    JOB,
    PREVIOUS_SOURCE,
    RESULT_ID,
    SESSION_ID,
    SOURCE,
    Harness,
    completed,
    context,
    contract_errors,
    event_errors,
)
from aggregator.inventory_store import InventoryIncompleteError
from inv_support import chunk_inventory, previous_object, put_inventory, static
from vlm_ai_client.client import AiGatewayError
from vlm_analysis_client.models import FinalState
from vlm_contracts.aggregates import result_key
from vlm_contracts.events import AnalysisCompleted, AnalysisCounts, SessionRef, SourceCompleted

type Item = dict[str, object]

KEY = result_key(SOURCE, JOB)
FAR = (0.7, 0.1, 0.2, 0.15)


def _moved_harness() -> Harness:
    h = Harness(empty_inventories=0)
    put_inventory(
        h.storage,
        chunk_inventory(CHUNK_A, 0, 60_000, static("s1", CHUNK_A, zone=AISLE, last=41_000)),
    )
    put_inventory(
        h.storage,
        chunk_inventory(
            CHUNK_B, 60_000, 120_000, static("s2", CHUNK_B, box=FAR, zone=AISLE, first=70_000)
        ),
    )
    return h


def _two_chunks() -> AnalysisCompleted:
    return completed().model_copy(
        update={"counts": AnalysisCounts(chunks=2, segments=2, events=0, tracks=0)}
    )


def _result(h: Harness) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(h.storage.get_bytes(KEY))
    return loaded


def _items(result: dict[str, object], name: str) -> list[Item]:
    return cast(list[Item], result[name])


def _sent(h: Harness) -> SourceCompleted:
    [event] = [e for e in h.publisher.of("source.completed") if isinstance(e, SourceCompleted)]
    return event


async def test_spec028_r2_fewer_inventory_files_than_chunks_retries_without_publishing() -> None:
    """[SPEC-028 R-2] 재고 파일 수가 counts.chunks보다 적으면 발행하지 않고 재시도한다."""
    h = Harness()  # 재고 파일 1개, counts.chunks 2

    with pytest.raises(InventoryIncompleteError):
        await h.service().on_analysis_completed(_two_chunks(), context(attempt=4))

    assert h.publisher.published == []
    assert not h.storage.exists(KEY)


async def test_spec028_r2_last_attempt_publishes_without_inventory_parts() -> None:
    """[SPEC-028 R-2] 재시도 상한에서도 모자라면 재고 단계 없이 나머지 결과를 발행한다."""
    h = Harness()

    await h.service().on_analysis_completed(_two_chunks(), context(attempt=5))

    result = _result(h)
    assert contract_errors(RESULT_ID, result) == []
    assert "inventory" not in result
    assert _sent(h).counts.inventory_events is None


async def test_spec028_r9_r13_moved_barrier_reaches_result_state_and_event() -> None:
    """[SPEC-028 R-9·R-13·R-14] 이동한 방호벽은 재고 이벤트·위험·예측이 되고 계약을 통과한다."""
    h = _moved_harness()

    await h.service().on_analysis_completed(_two_chunks(), context())

    result = _result(h)
    assert contract_errors(RESULT_ID, result) == []
    [change] = _items(result, "inventoryChanges")
    [stocked] = _items(result, "inventoryEvents")
    assert (change["kind"], change["eventId"]) == ("moved", stocked["eventId"])
    assert stocked["eventType"] == "object_moved"
    assert [o["className"] for o in _items(result, "inventory")] == ["safety_barrier"]
    [risk] = _items(result, "risks")
    assert (risk["riskType"], risk["zoneId"]) == ("barrier_not_restored", str(AISLE))
    [forecast] = _items(result, "forecasts")
    assert (forecast["riskId"], forecast["status"]) == (risk["riskId"], "generated")
    assert any(t["kind"] == "inventory_event" for t in _items(result, "timeline"))
    sent = _sent(h)
    assert event_errors(sent, "source.completed") == []
    assert (sent.counts.inventory_events, sent.counts.forecasts) == (1, 1)
    assert h.gateway.task_types() == ["state_compare", "risk_forecast", "source_summary"]


async def test_spec028_r6_previous_video_inventory_comes_from_final_state() -> None:
    """[SPEC-028 R-6] 세션 직전 영상 final-state의 staticObjects를 첫 청크 앞 재고로 놓는다."""
    h = Harness()
    before = previous_object("p1", source=PREVIOUS_SOURCE, zone=AISLE)
    h.storage.put_bytes(before.frame_key, b"jpeg", "image/jpeg")
    h.api.final_state = FinalState.model_validate(
        {
            "sourceId": str(PREVIOUS_SOURCE),
            "jobId": str(JOB),
            "zoneStates": [],
            "activeRisks": [],
            "staticObjects": [before.model_dump(mode="json", by_alias=True)],
        }
    )
    session = SessionRef(session_id=SESSION_ID, role="AFTER", previous_source_id=PREVIOUS_SOURCE)

    await h.service().on_analysis_completed(completed(session=session), context())

    [change] = _items(_result(h), "inventoryChanges")
    assert (change["kind"], change["beforeObjectId"]) == ("not_restored", "p1")


async def test_spec028_r15_forecast_failure_still_publishes_rest() -> None:
    """[SPEC-028 R-15] 예측이 실패해도 나머지 결과를 그대로 발행한다."""
    h = _moved_harness()
    h.gateway.task_errors["risk_forecast"] = AiGatewayError(
        "AI_UNAVAILABLE", "x", status=503, retryable=True
    )

    await h.service().on_analysis_completed(_two_chunks(), context())

    [forecast] = _items(_result(h), "forecasts")
    assert (forecast["status"], forecast["forecast"]) == ("failed", None)
    assert _sent(h).summary_status == "generated"


async def test_spec028_r18_inventory_stages_are_recorded_with_counts() -> None:
    """[SPEC-028 R-18] 단계 inventory_compare(객체 수)·judge(호출 수)·forecast(위험 수)를 남긴다."""
    h = _moved_harness()
    ctx = context()

    await h.service().on_analysis_completed(_two_chunks(), ctx)

    stages = {t.stage_id: (t.count, t.unit) for t in ctx.timing.timings}
    assert stages["aggregate.inventory_compare"] == (2, "objects")
    assert stages["aggregate.judge"] == (1, "calls")
    assert stages["aggregate.forecast"] == (1, "risks")


async def test_spec028_r17_same_input_gives_identical_result_file() -> None:
    """[SPEC-028 R-17] 같은 입력·프로필이면 재고 파일을 넣은 순서와 상관없이 결과 파일이 같다."""
    first = _moved_harness()
    second = Harness(empty_inventories=0)
    for key in reversed(first.storage.list_keys(f"sources/{SOURCE}/jobs/{JOB}/aggregate/")):
        second.storage.put_bytes(key, first.storage.get_bytes(key), "application/json")

    await first.service().on_analysis_completed(_two_chunks(), context())
    await second.service().on_analysis_completed(_two_chunks(), context())

    assert first.storage.get_bytes(KEY) == second.storage.get_bytes(KEY)
