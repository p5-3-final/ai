"""업로드 job 핸들러(SPEC-027 R-1~R-5, R-19~R-28, NFR-3)."""

import json
import logging
from pathlib import Path

import pytest
from agg_support import (
    CHUNK_A,
    JOB,
    PREVIOUS_SOURCE,
    RESULT_ID,
    SESSION_ID,
    SOURCE,
    SUMMARY_TEXT,
    T0,
    Harness,
    analysis,
    api_error,
    completed,
    context,
    contract_errors,
    contracts_dir,
    event,
    event_errors,
    job_event_json,
    person,
    risk_event,
    uid,
)
from aggregator.service import CountMismatchError
from vlm_ai_client.client import AiGatewayError
from vlm_contracts.aggregates import result_key
from vlm_contracts.events import EventAnalyzed, SessionRef, SourceCompleted
from vlm_messaging.worker import NonRetryableError
from vlm_storage.storage import ObjectNotFoundError

KEY = result_key(SOURCE, JOB)


def _with_events(h: Harness, *events: object) -> Harness:
    h.api.events = [job_event_json(e) for e in events]  # type: ignore[arg-type]
    return h


def _result(h: Harness) -> dict[str, object]:
    loaded: dict[str, object] = json.loads(h.storage.get_bytes(KEY))
    return loaded


def _completed_events(h: Harness) -> list[SourceCompleted]:
    return [e for e in h.publisher.of("source.completed") if isinstance(e, SourceCompleted)]


async def test_spec027_r1_full_job_stores_result_and_publishes_once() -> None:
    """[SPEC-027 R-1] FULL이면 결과 파일을 저장하고 source.completed를 한 건 발행한다.

    둘 다 계약 스키마를 통과한다.
    """
    h = _with_events(Harness(), risk_event(1, 30_000, detected=True))
    h.api.tracks = [person(10, 0, 100_000)]

    await h.service().on_analysis_completed(completed(events=1, tracks=1), context())

    assert contract_errors(RESULT_ID, _result(h)) == []
    [(topic, sent, key, trace)] = h.publisher.published
    assert (topic, key, trace) == ("source.completed", str(SOURCE), context().traceparent)
    assert isinstance(sent, SourceCompleted)
    assert event_errors(sent, "source.completed") == []
    assert (sent.result_key, sent.revision, sent.summary_status) == (KEY, 1, "generated")
    assert (sent.counts.input_events, sent.counts.confirmed_events, sent.counts.risks) == (1, 1, 1)


async def test_spec027_r1_reanalyze_is_aggregated_too() -> None:
    h = Harness()

    await h.service().on_analysis_completed(completed(job_type="REANALYZE"), context())

    assert len(_completed_events(h)) == 1


async def test_spec027_r2_reembed_is_skipped_with_reason(caplog: pytest.LogCaptureFixture) -> None:
    """[SPEC-027 R-2] REEMBED면 집계하지 않고 건너뛴 이유를 로그에 남긴다."""
    h = Harness()
    caplog.set_level(logging.INFO)

    await h.service().on_analysis_completed(completed(job_type="REEMBED"), context())

    assert (h.publisher.published, h.api.calls) == ([], [])
    assert any("REEMBED" in str(getattr(r, "reason", "")) for r in caplog.records)


async def test_spec027_us1_empty_job_publishes_empty_summary() -> None:
    """[SPEC-027 US-1] 확정 이벤트가 없으면 빈 목록과 요약 empty로 발행한다."""
    h = Harness()

    await h.service().on_analysis_completed(completed(), context())

    body = _result(h)
    assert (body["snapshots"], body["risks"]) == ([], [])
    assert _completed_events(h)[0].summary_status == "empty"
    assert h.gateway.requests == []


async def test_spec027_r3_every_call_uses_the_handler_deadline() -> None:
    """[SPEC-027 R-3] 모든 호출이 핸들러 시작 + 제한 시간(300초) 마감을 싣는다."""
    h = _with_events(Harness(), *(event(n, start=n) for n in range(1, 6)))

    await h.service().on_analysis_completed(
        completed(
            events=5,
            session=SessionRef(
                session_id=SESSION_ID, role="AFTER", previous_source_id=PREVIOUS_SOURCE
            ),
        ),
        context(),
    )

    assert {at for _, at, _ in h.api.calls} == {int((T0 + 300) * 1000)}
    assert {name for name, _, _ in h.api.calls} == {
        "events",
        "tracks",
        "presences",
        "zones",
        "final-state",
    }


async def test_spec027_r4_count_mismatch_is_retried_without_publishing() -> None:
    """[SPEC-027 R-4] rule* 이벤트 수가 counts와 다르면 발행하지 않고 재시도 가능 오류다."""
    h = _with_events(Harness(), event(1, start=0))

    with pytest.raises(CountMismatchError):
        await h.service().on_analysis_completed(completed(events=2), context(attempt=4))

    assert h.publisher.published == []
    assert not h.storage.exists(KEY)


async def test_spec027_r4_partial_on_last_attempt() -> None:
    """[SPEC-027 R-4] 재시도 상한(5회)에 닿으면 partial과 빠진 수를 실어 발행한다."""
    h = _with_events(Harness(), event(1, start=0))

    await h.service().on_analysis_completed(completed(events=2, tracks=1), context(attempt=5))

    [sent] = _completed_events(h)
    assert sent.partial is True
    assert sent.missing_counts is not None
    assert (sent.missing_counts.events, sent.missing_counts.tracks) == (1, 1)
    assert event_errors(sent, "source.completed") == []


async def test_spec027_r19_summary_failure_still_publishes_rest() -> None:
    """[SPEC-027 R-19] 요약 호출이 실패해도 나머지 결과는 그대로 발행한다."""
    h = _with_events(Harness(), risk_event(1, 30_000, detected=True))
    h.gateway.error = AiGatewayError("AI_UNAVAILABLE", "x", status=503, retryable=True)

    await h.service().on_analysis_completed(completed(events=1), context())

    assert _completed_events(h)[0].summary_status == "failed"
    assert len(_result(h)["risks"]) == 1  # type: ignore[arg-type]


async def test_spec027_r20_storage_failure_publishes_nothing() -> None:
    """[SPEC-027 R-20] 저장이 끝난 뒤 발행한다. 저장이 실패하면 발행 없이 오류를 그대로 던진다."""
    h = Harness()

    def broken(key: str, data: bytes, content_type: str) -> None:
        raise ObjectNotFoundError("bucket down")

    h.storage.put_bytes = broken  # type: ignore[method-assign]

    with pytest.raises(ObjectNotFoundError):
        await h.service().on_analysis_completed(completed(), context())

    assert h.publisher.published == []


@pytest.mark.parametrize(
    ("failure", "where"),
    [
        (api_error(503, "ANALYSIS_UNAVAILABLE"), "events"),
        (api_error(504, "ANALYSIS_DEADLINE_EXCEEDED"), "tracks"),
        (api_error(404, "ANALYSIS_JOB_NOT_FOUND"), "events"),
        (api_error(404, "ANALYSIS_SOURCE_NOT_FOUND"), "zones"),
    ],
)
async def test_spec027_r22_retryable_read_errors_propagate(failure: Exception, where: str) -> None:
    """[SPEC-027 R-22] 재시도 소진·504·404 job 없음은 그대로 던져 재시도 토픽으로 간다."""
    h = Harness()
    h.api.fail[where] = failure  # type: ignore[assignment]

    with pytest.raises(type(failure)):
        await h.service().on_analysis_completed(completed(), context())

    assert h.publisher.published == []


@pytest.mark.parametrize("code", ["ANALYSIS_INVALID_REQUEST", "ANALYSIS_INVALID_RESPONSE"])
async def test_spec027_r23_bad_request_or_invalid_response_is_non_retryable(code: str) -> None:
    """[SPEC-027 R-23] 400·계약과 맞지 않는 응답은 재시도 없이 DLQ다."""
    h = Harness()
    h.api.fail["events"] = api_error(400 if code.endswith("REQUEST") else 200, code)

    with pytest.raises(NonRetryableError):
        await h.service().on_analysis_completed(completed(), context())


async def test_spec027_r23_analysis_that_is_not_vlm_analysis_is_non_retryable() -> None:
    h = Harness()
    broken = job_event_json(event(1, start=0))
    broken["analysis"] = {"eventType": "fall"}
    h.api.events = [broken]

    with pytest.raises(NonRetryableError):
        await h.service().on_analysis_completed(completed(events=1), context())


async def test_spec027_r24_stages_are_recorded_with_counts() -> None:
    """[SPEC-027 R-24] aggregate.read·compute·summary·store 단계와 처리 수를 남긴다."""
    h = _with_events(Harness(), risk_event(1, 30_000, detected=True), event(2, start=0))
    ctx = context()

    await h.service().on_analysis_completed(completed(events=2), ctx)

    stages = {t.stage_id: t.count for t in ctx.timing.timings}
    assert stages == {
        "aggregate.read": 2,
        "aggregate.compute": 2,
        "aggregate.summary": 1,
        "aggregate.store": 1,
    }


def _analyzed(n: int, *, status: str = "confirmed", job_type: str | None = None) -> EventAnalyzed:
    vector_example = json.loads(
        (contracts_events() / "event.analyzed.schema.json").read_text(encoding="utf-8")
    )["examples"][0]
    body = {
        **vector_example,
        "eventId": str(uid(n)),
        "sourceId": str(SOURCE),
        "jobId": str(JOB),
        "chunkId": str(CHUNK_A),
        "startMs": 300_000,
        "endMs": 310_000,
        "analysis": json.loads(
            analysis(
                "fall", status, risk=True, risk_type="fall", risk_reason="쓰러짐"
            ).model_dump_json(by_alias=True)
        ),
    }
    if job_type:
        body["jobType"] = job_type
    return EventAnalyzed.model_validate(body)


def contracts_events() -> Path:
    return contracts_dir() / "events"


async def test_spec027_r25_r26_vector_event_waits_and_joins_the_next_aggregate() -> None:
    """[SPEC-027 R-25·R-26] 업로드 벡터 이벤트는 대기 집합에 넣고 집계 때 읽은 이벤트와 합친다."""
    h = Harness()
    service = h.service()

    await service.on_event_analyzed(_analyzed(50), context(topic="event.analyzed"))
    await service.on_analysis_completed(completed(), context())

    assert h.publisher.of("source.completed")
    [sent] = _completed_events(h)
    assert (sent.counts.input_events, sent.counts.risks) == (1, 1)


async def test_spec027_r25_rule_upload_events_are_ignored() -> None:
    h = Harness()
    rule_example = json.loads(
        (contracts_events() / "event.analyzed.schema.json").read_text(encoding="utf-8")
    )["examples"][1]

    await h.service().on_event_analyzed(EventAnalyzed.model_validate(rule_example), context())

    assert h.store is not None
    assert h.store.hashes == {}


async def test_spec027_r27_r28_late_confirmed_vector_event_republishes_next_revision() -> None:
    """[SPEC-027 R-27·R-28] 발행 뒤 온 confirmed 벡터 이벤트는 다시 계산해 revision+1로 낸다."""
    h = _with_events(Harness(), event(1, start=0))
    service = h.service()
    await service.on_analysis_completed(completed(events=1), context())

    await service.on_event_analyzed(_analyzed(60), context(topic="event.analyzed"))
    await service.on_event_analyzed(_analyzed(60), context(topic="event.analyzed"))

    first, second = _completed_events(h)
    assert (first.revision, second.revision) == (1, 2)
    assert first.event_id != second.event_id
    assert (second.counts.input_events, second.counts.risks) == (2, 1)
    meta = json.loads(h.storage.get_bytes(KEY.replace(".json", ".meta.json")))
    assert (meta["revision"], meta["inputEventIds"]) == (2, [str(uid(1)), str(uid(60))])


async def test_spec027_r27_unconfirmed_late_event_only_waits() -> None:
    h = Harness()
    service = h.service()
    await service.on_analysis_completed(completed(), context())

    await service.on_event_analyzed(_analyzed(61, status="unverified"), context())

    assert len(_completed_events(h)) == 1


async def test_spec027_nfr3_logs_have_no_summary_or_description_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[SPEC-027 NFR-3] 요약 문장과 이벤트 설명 원문을 로그에 남기지 않는다."""
    h = _with_events(Harness(), risk_event(1, 30_000, detected=True))
    h.gateway.output = {"summary": None, "highlights": []}
    caplog.set_level(logging.DEBUG)

    await h.service().on_analysis_completed(completed(events=1), context())
    h.gateway.output = {"summary": SUMMARY_TEXT, "highlights": []}
    await h.service().on_analysis_completed(completed(events=1), context())

    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert SUMMARY_TEXT not in logged
    assert "설명 원문" not in logged
