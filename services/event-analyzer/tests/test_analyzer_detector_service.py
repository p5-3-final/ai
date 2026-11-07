"""`event.candidate` 핸들러(SPEC-023): 병합, 규칙 확정, VLM 판정, event.analyzed·event.manifest."""

from dataclasses import replace

import pytest
from ea_support import (
    DETECTOR_PROFILE,
    NOW,
    PREFIX,
    PROFILE_PATH,
    SOURCE,
    TRACEPARENT,
    FakeGateway,
    MemoryRejudgeState,
    RecordingPublisher,
    candidate_file,
    detected,
    detector_message,
    event_errors,
    file_message,
    gateway_error,
    judged,
)
from event_analyzer.detector import DetectorAnalyzerService
from event_analyzer.ids import analyzed_event_id, manifest_event_id
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.service import AnalyzerConfig
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest
from vlm_common.profile import load_profile
from vlm_contracts.events import (
    DetectorCandidate,
    Event,
    EventAnalyzed,
    EventCandidate,
    EventManifest,
    VLMAnalysis,
)
from vlm_messaging.publisher import PublishError
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext, NonRetryableError
from vlm_storage.memory import InMemoryStorage

PROFILE = load_profile(PROFILE_PATH, EventAnalyzerProfile)
CTX = MessageContext(
    topic="event.candidate", partition=0, offset=0, key=None, attempt=1, traceparent=TRACEPARENT
)


def _service(
    gateway: FakeGateway,
    publisher: RecordingPublisher,
    *,
    profile: EventAnalyzerProfile = PROFILE,
    storage: InMemoryStorage | None = None,
    concurrency: int = 4,
    rejudge: MemoryRejudgeState | None = None,
) -> DetectorAnalyzerService:
    return DetectorAnalyzerService(
        gateway,
        publisher,
        storage or InMemoryStorage(),
        profile,
        AnalyzerConfig(concurrency=concurrency),
        rejudge=rejudge or MemoryRejudgeState(),
        clock=lambda: NOW,
    )


async def _run(
    message: EventCandidate,
    gateway: FakeGateway | None = None,
    *,
    ctx: MessageContext = CTX,
    profile: EventAnalyzerProfile = PROFILE,
    storage: InMemoryStorage | None = None,
    rejudge: MemoryRejudgeState | None = None,
) -> tuple[FakeGateway, list[EventAnalyzed], list[EventManifest]]:
    fake = gateway or FakeGateway()
    publisher = RecordingPublisher()
    service = _service(fake, publisher, profile=profile, storage=storage, rejudge=rejudge)
    await service.handle(message, ctx)
    analyzed: list[EventAnalyzed] = []
    manifests: list[EventManifest] = []
    for topic, event, key, traceparent in publisher.published:
        assert (key, traceparent) == (str(SOURCE), TRACEPARENT)
        assert topic == event.event_type
        _check(event)
        if isinstance(event, EventAnalyzed):
            assert not manifests, "event.manifest는 모든 event.analyzed 뒤에 발행한다"
            analyzed.append(event)
        else:
            assert isinstance(event, EventManifest)
            manifests.append(event)
    return fake, analyzed, manifests


def _check(event: Event) -> None:
    assert event_errors(event) == [], event


async def test_spec023_r8_us1_publishes_rule_vlm_event_per_vlm_candidate() -> None:
    first = detected(1, "ppe_missing", start_s=30, track="t3")
    second = detected(2, "fire_or_smoke", start_s=60, track=None)

    gateway, events, _ = await _run(detector_message(first, second))

    assert gateway.task_types() == ["event_judge", "event_judge"]
    assert [e.candidate_id for e in events] == [first.candidate_id, second.candidate_id]
    for event, source in zip(events, (first, second), strict=True):
        assert event.event_id == analyzed_event_id(source.candidate_id)
        assert event.judgment_method == "rule+vlm"
        assert event.analysis.model_dump(by_alias=True) == judged(source.event_type)
        assert (event.candidate_event_type, event.start_ms, event.end_ms) == (
            source.event_type,
            source.start_ms,
            source.end_ms,
        )
        assert event.evidence_frame_keys == source.evidence_frame_keys
        assert event.vector_candidate is None
        assert [j.task_type for j in event.judgments] == ["event_judge"]
        assert event.occurred_at == NOW


async def test_spec023_r4_us2_rule_candidate_is_published_without_gateway() -> None:
    rule = detected(1, "hazard_zone_access", judgment="rule", zone_name="A동 크레인 하부")

    gateway, [event], _ = await _run(detector_message(rule))

    assert gateway.requests == []
    assert event.judgment_method == "rule"
    assert event.judgments == []
    assert event.analysis.verification_status == "confirmed"
    assert event.analysis.description == "A동 크레인 하부 구역에 사람이 들어왔다."
    assert event.analysis.severity == PROFILE.rules["hazard_zone_access"].severity
    assert (event.review_required, event.review_reasons) == (False, [])


async def test_spec023_r3_us3_merged_candidates_are_judged_once() -> None:
    repeats = [detected(i, start_s=3 * i, score=s) for i, s in ((1, 0.6), (2, 0.9), (3, 0.7))]

    gateway, [event], _ = await _run(detector_message(*repeats))

    assert len(gateway.requests) == 1
    assert event.candidate_id == repeats[1].candidate_id
    assert event.detector_candidate is not None
    assert event.detector_candidate.merged_candidate_ids == [
        repeats[0].candidate_id,
        repeats[2].candidate_id,
    ]


async def test_spec023_r9_detector_candidate_carries_message_and_candidate_evidence() -> None:
    candidate = detected(1, "hazard_zone_access", judgment="rule", zone_name="B동", score=0.83)
    message = detector_message(candidate)

    _, [event], _ = await _run(message)

    assert event.detector_candidate is not None
    assert event.detector_candidate.model_dump() == {
        "chunk_id": message.chunk_id,
        "execution_id": message.execution_id,
        "trigger_class": candidate.trigger_class,
        "score": 0.83,
        "observation_count": candidate.observation_count,
        "track_local_id": candidate.track_local_id,
        "zone_id": candidate.zone_id,
        "zone_name": "B동",
        "detector_profile": DETECTOR_PROFILE,
        "merged_candidate_ids": [],
    }
    assert (event.job_id, event.chunk_id, event.execution_id, event.normalization_id) == (
        message.job_id,
        message.chunk_id,
        message.execution_id,
        message.normalization_id,
    )


async def test_spec023_r5_judge_request_has_frames_in_order_and_detector_evidence() -> None:
    candidate = detected(1, "ppe_missing", start_s=30, end_s=34, zone_name="C동", frames=3)

    gateway, _, _ = await _run(detector_message(candidate))

    [(request, traceparent)] = gateway.requests
    assert traceparent == TRACEPARENT
    assert request.task_type == "event_judge"
    assert request.source_id == SOURCE
    assert [i.storage_key for i in request.images] == candidate.evidence_frame_keys
    assert request.variables == {
        "eventType": "ppe_missing",
        "startSec": 30.0,
        "endSec": 34.0,
        "zoneName": "C동",
        "evidence": {
            "path": "detector",
            "triggerClass": candidate.trigger_class,
            "score": candidate.score,
            "observationCount": candidate.observation_count,
            "detectorProfile": DETECTOR_PROFILE,
        },
    }


async def test_spec023_r8_same_message_gives_same_event_ids() -> None:
    message = detector_message(detected(1), detected(2, start_s=50))

    _, once, _ = await _run(message)
    _, again, _ = await _run(message)

    assert [e.event_id for e in again] == [e.event_id for e in once]


async def test_spec023_r6_us1_high_severity_is_escalated_with_same_input() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object]:
        if request.task_type == "event_judge":
            return judged("fire_or_smoke", severity="high")
        return judged("fire_or_smoke", severity="high")

    gateway, [event], _ = await _run(
        detector_message(detected(1, "fire_or_smoke", track=None, score=0.9)),
        FakeGateway(reply=reply),
    )

    first, second = (r for r, _ in gateway.requests)
    assert second.model_dump() == {**first.model_dump(), "task_type": "event_judge_escalate"}
    assert event.judgment_method == "rule+vlm+escalate"
    assert [j.stage for j in event.judgments] == ["judge", "escalate"]


async def test_spec023_r6_ambiguous_score_is_escalated_even_when_first_is_low() -> None:
    # 로컬 프로필의 ppe_missing 애매 구간은 0.4~0.6이다.
    ambiguous = detected(1, "ppe_missing", score=0.5, track="t1")
    clear = detected(2, "ppe_missing", score=0.61, track="t2")
    gateway = FakeGateway(reply=lambda r: judged("ppe_missing", severity="low"))

    _, events, _ = await _run(detector_message(ambiguous, clear), gateway)

    methods = {e.candidate_id: e.judgment_method for e in events}
    assert methods == {
        ambiguous.candidate_id: "rule+vlm+escalate",
        clear.candidate_id: "rule+vlm",
    }


async def test_spec023_r10_manifest_follows_analyzed_events() -> None:
    repeats = [detected(i, start_s=3 * i) for i in (1, 2, 3)]
    rule = detected(4, "hazard_zone_access", judgment="rule", track="t5", zone_name="A")
    message = detector_message(*repeats, rule)

    _, events, [manifest] = await _run(message)

    assert manifest.event_id == manifest_event_id(message.execution_id)
    assert (manifest.candidate_count, manifest.merged_count, manifest.analyzed_count) == (4, 2, 2)
    assert manifest.analyzed_event_ids == [e.event_id for e in events]
    assert manifest.unsupported_events == message.unsupported_events
    assert manifest.detector_profile == DETECTOR_PROFILE
    assert (manifest.job_id, manifest.chunk_id, manifest.execution_id) == (
        message.job_id,
        message.chunk_id,
        message.execution_id,
    )


async def test_spec023_r10_manifest_event_id_is_stable_across_reprocessing() -> None:
    message = detector_message(detected(1))

    _, _, [once] = await _run(message)
    _, _, [again] = await _run(message)

    assert once.event_id == again.event_id


async def test_spec023_r11_us4_no_candidates_publishes_only_manifest() -> None:
    gateway, events, [manifest] = await _run(detector_message())

    assert gateway.requests == []
    assert events == []
    assert (manifest.candidate_count, manifest.analyzed_count) == (0, 0)
    assert manifest.analyzed_event_ids == []


async def test_spec023_r12_limits_concurrent_judgments() -> None:
    gateway = FakeGateway(delay_s=0.01)
    publisher = RecordingPublisher()
    # 10분 실행이라 상한(30회)에 걸리지 않는다.
    message = detector_message(
        *(detected(i, track=f"t{i}") for i in range(10)), owned_ms=(0, 600_000)
    )

    await _service(gateway, publisher, concurrency=3).handle(message, CTX)

    assert gateway.max_in_flight == 3
    assert len(gateway.requests) == 10


def _timed() -> MessageContext:
    return replace(CTX, timing=StageRecorder(service="event-analyzer", attempt=1, received_at=NOW))


async def test_spec023_r13_records_merge_judge_and_escalate_stages() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object]:
        risky = request.variables["eventType"] == "fire_or_smoke"
        return judged(str(request.variables["eventType"]), severity="high" if risky else "low")

    ctx = _timed()
    message = detector_message(
        detected(1, start_s=0, score=0.9),
        detected(2, start_s=3, score=0.8),
        detected(3, "fire_or_smoke", track=None),
        detected(4, "hazard_zone_access", judgment="rule", track="t9", zone_name="A"),
    )

    await _run(message, FakeGateway(reply=reply), ctx=ctx)

    assert [(t.stage_id, t.status, t.count, t.unit) for t in ctx.timing.timings] == [
        ("analyzer.merge", "ok", 4, "candidates"),
        ("analyzer.judge", "ok", 2, "candidates"),
        ("analyzer.escalate", "ok", 1, "candidates"),
    ]


async def test_spec023_r13_rule_only_or_empty_message_records_only_merge() -> None:
    for message in (
        detector_message(),
        detector_message(detected(1, "hazard_zone_access", judgment="rule", zone_name="A")),
    ):
        ctx = _timed()

        await _run(message, ctx=ctx)

        assert [t.stage_id for t in ctx.timing.timings] == ["analyzer.merge"]


async def test_spec023_r1_us4_file_candidates_are_handled_like_inline() -> None:
    candidates = [detected(1), detected(2, "hazard_zone_access", judgment="rule", track="t2")]
    storage = InMemoryStorage()
    storage.put_bytes(f"{PREFIX}candidates.json", candidate_file(*candidates), "application/json")

    _, events, [manifest] = await _run(file_message(*candidates), storage=storage)

    assert [e.candidate_id for e in events] == [c.candidate_id for c in candidates]
    assert [e.judgment_method for e in events] == ["rule+vlm", "rule"]
    assert manifest.candidate_count == 2


async def test_spec023_r2_us4_missing_file_publishes_nothing() -> None:
    publisher = RecordingPublisher()

    with pytest.raises(NonRetryableError):
        await _service(FakeGateway(), publisher).handle(file_message(detected(1)), CTX)

    assert publisher.published == []


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_PROVIDER_UNAVAILABLE", 503, retryable=True),
        gateway_error("AI_TIMEOUT", None, retryable=True),
        gateway_error("AI_DEADLINE_EXCEEDED", 504),
        gateway_error("AI_IMAGE_NOT_FOUND", 404),
        gateway_error("AI_RATE_LIMITED", 429),
    ],
)
async def test_spec023_r7_failed_first_call_is_unknown(error: AiGatewayError) -> None:
    gateway = FakeGateway(reply=lambda r: error)

    _, [event], [manifest] = await _run(detector_message(detected(1, "fall")), gateway)

    assert event.analysis == VLMAnalysis.unknown("fall")
    assert event.judgment_method == "rule+vlm"
    [judgment] = event.judgments
    assert (judgment.error_code, judgment.call_id) == (error.code, None)
    assert manifest.analyzed_count == 1


async def test_spec023_r7_failed_escalation_keeps_first_result() -> None:
    def reply(request: AnalyzeRequest) -> dict[str, object] | AiGatewayError:
        if request.task_type == "event_judge_escalate":
            return gateway_error("AI_PROVIDER_FAILED", 502, retryable=True)
        return judged("fall", severity="high")

    _, [event], _ = await _run(detector_message(detected(1, "fall")), FakeGateway(reply=reply))

    assert event.analysis.model_dump(by_alias=True) == judged("fall", severity="high")
    assert event.judgment_method == "rule+vlm+escalate"
    assert event.judgments[1].error_code == "AI_PROVIDER_FAILED"


async def test_spec023_r7_other_client_error_publishes_neither_analysis_nor_manifest() -> None:
    publisher = RecordingPublisher()
    gateway = FakeGateway(reply=lambda r: gateway_error("AI_CALLER_FORBIDDEN", 403))
    message = detector_message(detected(1), detected(2, "hazard_zone_access", judgment="rule"))

    with pytest.raises(NonRetryableError):
        await _service(gateway, publisher).handle(message, CTX)

    assert publisher.published == []


class _FlakyPublisher(RecordingPublisher):
    """처음 `fail_after`건 뒤에 한 번 실패하는 발행기."""

    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self._fail_after = fail_after
        self._failed = False

    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        if not self._failed and len(self.published) == self._fail_after:
            self._failed = True
            raise PublishError("broker down")
        super().publish(topic, event, key=key, traceparent=traceparent)


async def test_spec023_r15_publish_failure_is_raised_and_retry_uses_same_ids() -> None:
    message = detector_message(detected(1), detected(2, start_s=50))
    publisher = _FlakyPublisher(fail_after=1)
    service = _service(FakeGateway(), publisher)

    with pytest.raises(PublishError):
        await service.handle(message, CTX)
    await service.handle(message, CTX)

    ids = [e.event_id for _, e, _, _ in publisher.published]
    # 첫 시도의 1건 + 다시 처리한 분석 2건과 manifest
    assert ids[0] == ids[1]
    assert len(ids) == 4
    assert ids[3] == manifest_event_id(message.execution_id)


async def test_spec023_r17_stateful_repeats_within_interval_are_carried() -> None:
    first, between, later = (detected(i, start_s=s) for i, s in ((1, 0), (2, 30), (3, 200)))

    gateway, events, _ = await _run(detector_message(first, between, later))

    assert len(gateway.requests) == 2
    by_id = {e.candidate_id: e for e in events}
    carried = by_id[between.candidate_id]
    assert carried.judgment_method == "rule+carry"
    assert carried.judgments == []
    assert carried.carried_from_event_id == analyzed_event_id(first.candidate_id)
    assert carried.analysis == by_id[first.candidate_id].analysis
    assert by_id[later.candidate_id].judgment_method == "rule+vlm"


async def test_spec023_r17_carry_copies_unknown_state_of_the_judged_candidate() -> None:
    gateway = FakeGateway(reply=lambda r: gateway_error("AI_TIMEOUT", None, retryable=True))

    _, [judged_event, carried], _ = await _run(
        detector_message(detected(1, start_s=0), detected(2, start_s=30)), gateway
    )

    assert carried.analysis == judged_event.analysis == VLMAnalysis.unknown("ppe_missing")
    assert carried.judgment_method == "rule+carry"


async def test_spec023_r19_us3_candidates_beyond_cap_are_unverified_without_call() -> None:
    # 2분 청크의 상한은 6회다. 시작이 늦은 2개가 상한에 걸린다.
    candidates = [detected(i, "fall", start_s=10 * i, track=f"t{i}") for i in range(8, 0, -1)]

    gateway, events, _ = await _run(detector_message(*candidates))

    assert len(gateway.requests) == 6
    capped = {e.candidate_id.int for e in events if e.unverified_reason == "call_cap_reached"}
    assert capped == {0xD007, 0xD008}
    for event in events:
        if event.candidate_id.int in capped:
            assert event.analysis.verification_status == "unverified"
            assert event.judgment_method == "rule+vlm"
            [skipped] = event.judgments
            assert (skipped.call_id, skipped.verification_status) == (None, "unverified")
            assert skipped.error_code == "CALL_CAP_REACHED"


async def test_spec023_r19_escalation_only_uses_remaining_calls() -> None:
    # 20초 창의 상한은 3회다. 1차 2회 뒤 재판정은 1회만 남는다.
    candidates = [detected(i, "fire_or_smoke", start_s=i, track=None) for i in (1, 2)]
    gateway = FakeGateway(reply=lambda r: judged("fire_or_smoke", severity="high"))

    _, events, _ = await _run(detector_message(*candidates, owned_ms=(100_000, 120_000)), gateway)

    assert gateway.task_types() == ["event_judge", "event_judge", "event_judge_escalate"]
    methods = {e.candidate_id.int: e.judgment_method for e in events}
    assert methods == {0xD001: "rule+vlm+escalate", 0xD002: "rule+vlm"}
    assert all(e.unverified_reason is None for e in events)


async def test_spec023_r19_rule_and_carried_candidates_do_not_use_calls() -> None:
    candidates = [
        *(detected(i, "hazard_zone_access", judgment="rule", track=f"r{i}") for i in range(5)),
        *(detected(10 + i, start_s=20 * i) for i in range(5)),
    ]

    gateway, events, _ = await _run(detector_message(*candidates, owned_ms=(0, 20_000)))

    assert len(gateway.requests) == 1
    assert not any(e.unverified_reason for e in events)


async def test_spec023_r20_manifest_counts_carry_cap_and_budget() -> None:
    # 20초 창의 상한 3회: 보호구 1회(+복사 1건), 화재 2회는 예산 초과, 남은 화재 1건은 상한
    candidates = [
        detected(1, start_s=0),
        detected(2, start_s=15),
        *(detected(10 + i, "fire_or_smoke", start_s=i + 1, track=None) for i in range(3)),
    ]

    def reply(request: AnalyzeRequest) -> dict[str, object]:
        if request.variables["eventType"] == "fire_or_smoke":
            return judged("fire_or_smoke", "unverified")
        return judged("ppe_missing")

    gateway = FakeGateway(reply=reply, unverified_reason="budget_exceeded")
    message = detector_message(*candidates, job_type="STREAM", owned_ms=(0, 20_000))

    _, events, [manifest] = await _run(message, gateway)

    assert (manifest.carried_count, manifest.cap_reached_count) == (1, 1)
    assert manifest.budget_exceeded_count == 2
    assert manifest.job_type == "STREAM"
    assert len(events) == 5


async def test_spec023_r20_counts_are_zero_not_missing() -> None:
    _, _, [manifest] = await _run(detector_message())

    body = manifest.model_dump(by_alias=True, mode="json")
    assert (body["carriedCount"], body["capReachedCount"], body["budgetExceededCount"]) == (0, 0, 0)
    assert "jobType" not in body


def _window(index: int, *candidates: DetectorCandidate) -> EventCandidate:
    """실시간 20초 창 `index`."""
    return detector_message(
        *candidates, job_type="STREAM", owned_ms=(index * 20_000, (index + 1) * 20_000)
    )


async def test_spec023_r18_stream_carries_across_windows_from_last_confirmed() -> None:
    state = MemoryRejudgeState()
    first_window = _window(0, detected(1, start_s=5))
    second = _window(1, detected(2, start_s=25))

    gateway1, [judged_event], _ = await _run(first_window, rejudge=state)
    gateway2, [carried], [manifest] = await _run(second, rejudge=state)

    assert len(gateway1.requests) == 1
    assert gateway2.requests == []
    assert carried.judgment_method == "rule+carry"
    assert carried.carried_from_event_id == judged_event.event_id
    assert carried.analysis == judged_event.analysis
    assert manifest.carried_count == 1
    key = (SOURCE, "t1", "ppe_missing")
    assert state.values[key].start_ms == 5000
    # TTL은 재판정 간격(120초)의 두 배다.
    assert state.ttls[key] == 240


async def test_spec023_r18_stream_judges_again_after_interval() -> None:
    state = MemoryRejudgeState()
    await _run(_window(0, detected(1, start_s=5)), rejudge=state)
    late = _window(7, detected(2, start_s=145))

    gateway, [event], _ = await _run(late, rejudge=state)

    assert len(gateway.requests) == 1
    assert event.judgment_method == "rule+vlm"
    assert state.values[(SOURCE, "t1", "ppe_missing")].start_ms == 145_000


async def test_spec023_r18_only_confirmed_judgments_update_the_state() -> None:
    state = MemoryRejudgeState()
    gateway = FakeGateway(reply=lambda r: judged("ppe_missing", "rejected", severity="low"))

    await _run(_window(0, detected(1, start_s=5)), gateway, rejudge=state)

    assert state.values == {}


async def test_spec023_r18_upload_jobs_do_not_use_the_state() -> None:
    state = MemoryRejudgeState()

    await _run(detector_message(detected(1, start_s=5)), rejudge=state)

    assert state.values == {}
