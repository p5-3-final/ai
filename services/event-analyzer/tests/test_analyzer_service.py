from dataclasses import replace

import pytest
from ea_support import (
    EMBEDDING_MODEL,
    NOW,
    PROTOTYPE_VERSION,
    SOURCE,
    TRACEPARENT,
    FakeGateway,
    RecordingPublisher,
    candidate,
    candidates_message,
    event_errors,
    gateway_error,
    judged,
)
from event_analyzer.service import AnalyzerConfig, EventAnalyzerService
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest
from vlm_contracts.events import Event, EventAnalyzed, EventCandidateVector, VLMAnalysis
from vlm_messaging.publisher import PublishError
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError

CTX = MessageContext(
    topic="event.candidate.vector",
    partition=0,
    offset=0,
    key=None,
    attempt=1,
    traceparent=TRACEPARENT,
)


def _service(
    gateway: FakeGateway, publisher: RecordingPublisher, concurrency: int = 4
) -> EventAnalyzerService:
    return EventAnalyzerService(
        gateway, publisher, AnalyzerConfig(concurrency=concurrency), clock=lambda: NOW
    )


async def _run(
    message: EventCandidateVector,
    gateway: FakeGateway | None = None,
    ctx: MessageContext = CTX,
) -> tuple[FakeGateway, list[EventAnalyzed]]:
    fake = gateway or FakeGateway()
    publisher = RecordingPublisher()
    await _service(fake, publisher).handle(message, ctx)
    events: list[EventAnalyzed] = []
    for topic, event, _, traceparent in publisher.published:
        assert (topic, traceparent) == ("event.analyzed", TRACEPARENT)
        assert isinstance(event, EventAnalyzed)
        events.append(event)
    return fake, events


async def test_spec020_r1_r2_judges_each_candidate_with_its_frames_and_evidence() -> None:
    first, second = candidate(0, "fall"), candidate(3, "fire_or_smoke")

    gateway, _ = await _run(candidates_message(first, second))

    requests = sorted((r for r, _ in gateway.requests), key=lambda r: r.images[0].storage_key)
    assert [r.task_type for r in requests] == ["event_judge", "event_judge"]
    for request, source in zip(requests, (first, second), strict=True):
        assert request.source_id == SOURCE
        assert [i.storage_key for i in request.images] == source.frame_keys
        assert request.variables == {
            "eventType": source.event_type,
            "startSec": source.start_ms / 1000,
            "endSec": source.end_ms / 1000,
            "zoneName": None,
            "evidence": {
                "path": "vector",
                "score": source.score,
                "threshold": source.threshold,
                "prototypeVersion": PROTOTYPE_VERSION,
                "embeddingModel": EMBEDDING_MODEL,
            },
        }
    assert {t for _, t in gateway.requests} == {TRACEPARENT}


async def test_spec020_r8_r10_publishes_one_contract_event_per_candidate() -> None:
    first, second = candidate(0, "fall"), candidate(3, "fire_or_smoke")
    message = candidates_message(first, second)

    _, events = await _run(message)

    by_candidate = {e.candidate_id: e for e in events}
    assert set(by_candidate) == {first.candidate_id, second.candidate_id}
    for source in (first, second):
        event = by_candidate[source.candidate_id]
        assert event_errors(event) == []
        assert (event.source_id, event.owner_id) == (message.source_id, message.owner_id)
        assert (event.job_id, event.chunk_id, event.execution_id, event.normalization_id) == (
            message.job_id,
            message.chunk_id,
            message.execution_id,
            message.normalization_id,
        )
        assert (event.candidate_event_type, event.start_ms, event.end_ms) == (
            source.event_type,
            source.start_ms,
            source.end_ms,
        )
        assert event.evidence_frame_keys == source.frame_keys
        assert event.vector_candidate is not None
        assert event.vector_candidate.model_dump() == {
            "segment_id": source.segment_id,
            "score": source.score,
            "threshold": source.threshold,
            "prototype_version": PROTOTYPE_VERSION,
            "embedding_model": EMBEDDING_MODEL,
        }
        assert event.analysis.model_dump(by_alias=True) == judged(source.event_type)
        assert event.judgment_method == "vector+vlm"
        [judgment] = event.judgments
        assert (judgment.stage, judgment.task_type, judgment.verification_status) == (
            "judge",
            "event_judge",
            "confirmed",
        )
        assert judgment.call_id is not None
        assert (judgment.model_id, judgment.prompt_version, judgment.error_code) == (
            "event_judge-m",
            "event_judge@0123456789ab",
            None,
        )
        assert (event.review_required, event.review_reasons) == (False, [])
        assert event.occurred_at == NOW


async def test_spec020_r9_same_message_gives_same_event_ids() -> None:
    message = candidates_message(candidate(0), candidate(1))

    _, once = await _run(message)
    _, again = await _run(message)

    ids = {e.candidate_id: e.event_id for e in once}
    assert {e.candidate_id: e.event_id for e in again} == ids
    assert len(set(ids.values())) == 2


async def test_spec020_us1_unverified_is_published_without_escalation() -> None:
    gateway = FakeGateway(reply=lambda r: judged(str(r.variables["eventType"]), "unverified"))

    _, [event] = await _run(candidates_message(candidate(0)), gateway)

    assert gateway.task_types() == ["event_judge"]
    assert event.analysis.verification_status == "unverified"
    assert event.judgments[0].model_id is None
    assert event.judgment_method == "vector+vlm"
    assert event_errors(event) == []


async def test_spec020_r3_high_severity_is_escalated_with_same_input() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object]:
        if request.task_type == "event_judge":
            return judged("fall", severity="high")
        return judged("fall", "rejected", severity="low")

    gateway = FakeGateway(reply=reply)

    _, [event] = await _run(candidates_message(candidate(0)), gateway)

    first, second = (r for r, _ in gateway.requests)
    assert (first.task_type, second.task_type) == ("event_judge", "event_judge_escalate")
    assert second.model_dump() == {**first.model_dump(), "task_type": "event_judge_escalate"}
    assert event.judgment_method == "vector+vlm+escalate"
    assert event.analysis.verification_status == "rejected"
    assert [j.task_type for j in event.judgments] == ["event_judge", "event_judge_escalate"]
    # 1차 confirmed, 재판정 rejected라 사람이 본다(R-7).
    assert (event.review_required, event.review_reasons) == (True, ["judgment_disagreement"])
    assert event_errors(event) == []


async def test_spec020_r3_only_risky_candidates_are_escalated() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object]:
        risky = request.variables["eventType"] == "fire_or_smoke"
        return judged(str(request.variables["eventType"]), severity="high" if risky else "low")

    gateway = FakeGateway(reply=reply)

    _, events = await _run(
        candidates_message(candidate(0, "fall"), candidate(1, "fire_or_smoke")), gateway
    )

    assert sorted(gateway.task_types()) == ["event_judge", "event_judge", "event_judge_escalate"]
    methods = {e.candidate_event_type: e.judgment_method for e in events}
    assert methods == {"fall": "vector+vlm", "fire_or_smoke": "vector+vlm+escalate"}


async def test_spec020_r18_limits_concurrent_judgments() -> None:
    gateway = FakeGateway(delay_s=0.01)
    publisher = RecordingPublisher()
    message = candidates_message(*(candidate(i) for i in range(10)))

    await _service(gateway, publisher, concurrency=3).handle(message, CTX)

    assert gateway.max_in_flight == 3
    assert len(publisher.published) == 10


def _timed_ctx() -> MessageContext:
    return replace(CTX, timing=StageRecorder(service="event-analyzer", attempt=1, received_at=NOW))


async def test_spec020_r19_records_judge_and_escalate_stages() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object]:
        risky = request.variables["eventType"] == "fire_or_smoke"
        return judged(str(request.variables["eventType"]), severity="high" if risky else "low")

    ctx = _timed_ctx()
    message = candidates_message(
        candidate(0, "fall"), candidate(1, "fall"), candidate(2, "fire_or_smoke")
    )

    await _run(message, FakeGateway(reply=reply), ctx)

    assert [(t.stage_id, t.status, t.count, t.unit) for t in ctx.timing.timings] == [
        ("analyzer.judge", "ok", 3, "candidates"),
        ("analyzer.escalate", "ok", 1, "candidates"),
    ]


async def test_spec020_r19_no_escalate_stage_without_escalation() -> None:
    ctx = _timed_ctx()

    await _run(candidates_message(candidate(0)), FakeGateway(), ctx)

    assert [t.stage_id for t in ctx.timing.timings] == ["analyzer.judge"]


@pytest.mark.parametrize("count", [1, 12])
async def test_spec020_r8_every_candidate_is_published(count: int) -> None:
    _, events = await _run(candidates_message(*(candidate(i) for i in range(count))))

    assert len(events) == count


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (gateway_error("AI_PROVIDER_UNAVAILABLE", 503, retryable=True), "AI_PROVIDER_UNAVAILABLE"),
        (gateway_error("AI_TIMEOUT", None, retryable=True), "AI_TIMEOUT"),
        (gateway_error("AI_IMAGE_NOT_FOUND", 404), "AI_IMAGE_NOT_FOUND"),
        (gateway_error("AI_RATE_LIMITED", 429), "AI_RATE_LIMITED"),
        (gateway_error("AI_INVALID_RESPONSE", 200), "AI_INVALID_RESPONSE"),
    ],
)
async def test_spec020_r12_r14_failed_first_call_is_published_as_unknown(
    error: AiGatewayError, code: str
) -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object] | AiGatewayError:
        failing = request.variables["eventType"] == "ppe_missing"
        return error if failing else judged(str(request.variables["eventType"]))

    gateway = FakeGateway(reply=reply)

    _, events = await _run(
        candidates_message(candidate(0, "ppe_missing"), candidate(1, "fall")), gateway
    )

    by_type = {e.candidate_event_type: e for e in events}
    failed = by_type["ppe_missing"]
    assert failed.analysis == VLMAnalysis.unknown("ppe_missing")
    assert failed.judgment_method == "vector+vlm"
    [judgment] = failed.judgments
    assert (judgment.call_id, judgment.model_id, judgment.prompt_version) == (None, None, None)
    assert (judgment.verification_status, judgment.error_code) == ("unknown", code)
    assert event_errors(failed) == []
    # 다른 후보는 정상 판정된다(US-3).
    assert by_type["fall"].analysis.verification_status == "confirmed"
    # 실패한 1차 판정은 재판정하지 않는다(R-4).
    assert gateway.task_types().count("event_judge_escalate") == 0


async def test_spec020_r12_output_not_matching_vlm_analysis_is_a_failed_call() -> None:
    gateway = FakeGateway(reply=lambda r: {"eventType": "fall"})

    _, [event] = await _run(candidates_message(candidate(0)), gateway)

    assert event.analysis.verification_status == "unknown"
    assert event.judgments[0].error_code == "AI_INVALID_RESPONSE"


async def test_spec020_r13_failed_escalation_keeps_first_result() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object] | AiGatewayError:
        if request.task_type == "event_judge_escalate":
            return gateway_error("AI_PROVIDER_FAILED", 502, retryable=True)
        return judged("fall", severity="high")

    gateway = FakeGateway(reply=reply)

    _, [event] = await _run(candidates_message(candidate(0)), gateway)

    assert event.analysis.model_dump(by_alias=True) == judged("fall", severity="high")
    assert event.judgment_method == "vector+vlm+escalate"
    escalate = event.judgments[1]
    assert (escalate.task_type, escalate.error_code, escalate.call_id) == (
        "event_judge_escalate",
        "AI_PROVIDER_FAILED",
        None,
    )
    assert event_errors(event) == []


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("event_judge", gateway_error("AI_CALLER_FORBIDDEN", 403)),
        ("event_judge", gateway_error("AI_UNKNOWN_TASK_TYPE", 400)),
        ("event_judge_escalate", gateway_error("AI_IMAGE_PATH_FORBIDDEN", 403)),
    ],
)
async def test_spec020_r15_other_client_error_publishes_nothing(
    stage: str, error: AiGatewayError
) -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object] | AiGatewayError:
        return error if request.task_type == stage else judged("fall", severity="high")

    publisher = RecordingPublisher()
    service = _service(FakeGateway(reply=reply), publisher)

    with pytest.raises(NonRetryableError):
        await service.handle(candidates_message(candidate(0), candidate(1)), CTX)

    assert publisher.published == []


class _BrokenPublisher(RecordingPublisher):
    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        raise PublishError("broker down")


async def test_spec020_r17_publish_failure_is_raised_for_retry() -> None:
    service = _service(FakeGateway(), _BrokenPublisher())

    with pytest.raises(PublishError):
        await service.handle(candidates_message(candidate(0)), CTX)


async def test_spec021_r14_deadline_exceeded_is_recorded_as_failed_call() -> None:
    """[SPEC-021 R-14] 504 AI_DEADLINE_EXCEEDED는 메시지를 거절하지 않고 그 판정의 실패로 남긴다."""
    gateway = FakeGateway(reply=lambda r: gateway_error("AI_DEADLINE_EXCEEDED", 504))

    _, [event] = await _run(candidates_message(candidate(0)), gateway)

    assert event.analysis.verification_status == "unknown"
    [judgment] = event.judgments
    assert (judgment.error_code, judgment.call_id) == ("AI_DEADLINE_EXCEEDED", None)
    assert gateway.task_types() == ["event_judge"]
