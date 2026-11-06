import logging
from dataclasses import replace
from uuid import UUID

import pytest
from ta_support import (
    ANSWER,
    ATTRIBUTES,
    NOW,
    QUESTION,
    SOURCE,
    TRACEPARENT,
    FakeGateway,
    RecordingPublisher,
    Reply,
    attribute_request,
    crop,
    event_errors,
    gateway_error,
    scene_key,
)
from track_analyzer.service import TrackAnalyzerService
from vlm_ai_client.client import AiGatewayError
from vlm_contracts.events import AttributeReason, AttributeRequested, Event, TrackAnalyzed
from vlm_messaging.publisher import PublishError
from vlm_messaging.timing import StageRecorder
from vlm_messaging.worker import MessageContext

CTX = MessageContext(
    topic="attribute.requested",
    partition=0,
    offset=0,
    key=None,
    attempt=1,
    traceparent=TRACEPARENT,
)


async def _run(
    message: AttributeRequested,
    gateway: FakeGateway | None = None,
    ctx: MessageContext = CTX,
) -> tuple[FakeGateway, RecordingPublisher]:
    fake = gateway or FakeGateway()
    publisher = RecordingPublisher()
    await TrackAnalyzerService(fake, publisher, clock=lambda: NOW).handle(message, ctx)
    return fake, publisher


def _published(publisher: RecordingPublisher) -> TrackAnalyzed:
    [(topic, event, key, traceparent)] = publisher.published
    assert (topic, key, traceparent) == ("track.analyzed", str(SOURCE), TRACEPARENT)
    assert isinstance(event, TrackAnalyzed)
    assert event_errors(event) == []
    return event


async def test_spec024_r1_click_calls_track_attribute_with_crops_in_order() -> None:
    gateway, _ = await _run(attribute_request("click", crops=3))

    [(request, traceparent)] = gateway.requests
    assert request.task_type == "track_attribute"
    assert request.source_id == SOURCE
    assert [(i.storage_key, i.pts_sec) for i in request.images] == [
        (crop(i).key, crop(i).pts_ms / 1000) for i in range(3)
    ]
    assert request.variables == {"className": "person"}
    assert traceparent == TRACEPARENT


async def test_spec024_r1_event_reason_also_calls_track_attribute() -> None:
    gateway, _ = await _run(attribute_request("event", crops=1))

    assert [r.task_type for r, _ in gateway.requests] == ["track_attribute"]


async def test_spec024_r2_question_calls_object_question_with_crops_then_scenes() -> None:
    gateway, _ = await _run(attribute_request("question", crops=2, scenes=3))

    [(request, _)] = gateway.requests
    assert request.task_type == "object_question"
    assert [(i.storage_key, i.pts_sec) for i in request.images] == [
        (crop(0).key, crop(0).pts_ms / 1000),
        (crop(1).key, crop(1).pts_ms / 1000),
        (scene_key(0), None),
        (scene_key(1), None),
        (scene_key(2), None),
    ]
    assert request.variables == {"className": "person", "question": QUESTION}


@pytest.mark.parametrize("reason", ["event", "click"])
async def test_spec024_r3_attribute_result_is_published_ready(reason: AttributeReason) -> None:
    message = attribute_request(reason)

    _, publisher = await _run(message)

    event = _published(publisher)
    assert (event.outcome, event.reason) == ("READY", reason)
    assert (event.request_id, event.job_id, event.track_id) == (
        message.request_id,
        message.job_id,
        message.track_id,
    )
    assert (event.source_id, event.owner_id) == (message.source_id, message.owner_id)
    assert event.attributes is not None
    assert event.attributes.model_dump(by_alias=True) == ATTRIBUTES
    assert event.answer is None
    assert (event.call_id, event.model_id, event.prompt_version, event.taxonomy_version) == (
        UUID(int=1),
        "track_attribute-m",
        "track_attribute@0123456789ab",
        "v2",
    )
    assert event.error_code is None


async def test_spec024_r3_answer_is_published_ready() -> None:
    _, publisher = await _run(attribute_request("question", scenes=1))

    event = _published(publisher)
    assert (event.outcome, event.reason, event.attributes) == ("READY", "question", None)
    assert event.answer is not None
    assert event.answer.model_dump(by_alias=True) == ANSWER
    assert event.prompt_version == "object_question@0123456789ab"


_UNVERIFIED_ATTRIBUTES: dict[str, object] = {
    "verificationStatus": "unverified",
    "helmet": None,
    "vest": None,
    "role": None,
    "equipment": [],
    "description": None,
}


@pytest.mark.parametrize("status", ["unverified", "unknown"])
async def test_spec024_r3_unverified_and_unknown_are_still_ready(status: str) -> None:
    output = {**_UNVERIFIED_ATTRIBUTES, "verificationStatus": status}

    _, publisher = await _run(attribute_request("click"), FakeGateway(reply=lambda _: output))

    event = _published(publisher)
    assert event.outcome == "READY"
    assert event.attributes is not None
    assert event.attributes.verification_status == status


async def test_spec024_r5_same_request_gets_same_event_id() -> None:
    message = attribute_request("click")

    _, first = await _run(message)
    _, second = await _run(message)
    _, other = await _run(attribute_request("click", request_id=UUID(int=99)))

    assert _published(first).event_id == _published(second).event_id
    assert _published(other).event_id != _published(first).event_id


def _timed_ctx() -> MessageContext:
    return replace(CTX, timing=StageRecorder(service="track-analyzer", attempt=1, received_at=NOW))


@pytest.mark.parametrize(("reason", "scenes", "count"), [("click", 0, 2), ("question", 3, 5)])
async def test_spec024_r10_records_call_stage_with_image_count(
    reason: AttributeReason, scenes: int, count: int
) -> None:
    ctx = _timed_ctx()

    await _run(attribute_request(reason, scenes=scenes), ctx=ctx)

    assert [(t.stage_id, t.status, t.count, t.unit) for t in ctx.timing.timings] == [
        ("attribute.call", "ok", count, "images"),
    ]


@pytest.mark.parametrize(
    "error",
    [
        gateway_error("AI_PROVIDER_UNAVAILABLE", 503),
        gateway_error("AI_DEADLINE_EXCEEDED", 504),
        gateway_error("AI_TIMEOUT", None),
        gateway_error("AI_CONNECTION_FAILED", None, retryable=True),
        gateway_error("AI_CALLER_FORBIDDEN", 403),
        gateway_error("AI_IMAGE_NOT_FOUND", 400),
        gateway_error("AI_RATE_LIMITED", 429),
    ],
)
@pytest.mark.parametrize("reason", ["click", "question"])
async def test_spec024_r4_call_without_result_is_published_failed(
    error: AiGatewayError, reason: AttributeReason
) -> None:
    gateway = FakeGateway(reply=lambda _: error)

    _, publisher = await _run(attribute_request(reason), gateway)

    # 다시 부르지 않는다. 연결 실패 재시도는 클라이언트 안의 일이다.
    assert len(gateway.requests) == 1
    event = _published(publisher)
    assert (event.outcome, event.error_code) == ("FAILED", error.code)
    assert (event.attributes, event.answer) == (None, None)
    assert (event.call_id, event.model_id, event.prompt_version, event.taxonomy_version) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("reason", "output"),
    [
        ("click", {"verificationStatus": "confirmed"}),
        ("click", {**ATTRIBUTES, "helmet": "maybe"}),
        ("question", ATTRIBUTES),
    ],
)
async def test_spec024_r4_output_not_matching_result_shape_is_failed(
    reason: AttributeReason, output: dict[str, object]
) -> None:
    _, publisher = await _run(attribute_request(reason), FakeGateway(reply=lambda _: output))

    event = _published(publisher)
    assert (event.outcome, event.error_code) == ("FAILED", "AI_INVALID_RESPONSE")


async def test_spec024_r4_unknown_taxonomy_version_is_failed() -> None:
    _, publisher = await _run(attribute_request("click"), FakeGateway(taxonomy_version="v3"))

    assert _published(publisher).error_code == "AI_INVALID_RESPONSE"


async def test_spec024_r4_failed_call_stage_is_recorded_as_error() -> None:
    ctx = _timed_ctx()

    await _run(
        attribute_request("click"),
        FakeGateway(reply=lambda _: gateway_error("AI_PROVIDER_UNAVAILABLE", 503)),
        ctx,
    )

    assert [(t.stage_id, t.status) for t in ctx.timing.timings] == [("attribute.call", "error")]


class _DownPublisher(RecordingPublisher):
    def publish(
        self, topic: str, event: Event, *, key: str | None = None, traceparent: str | None = None
    ) -> None:
        raise PublishError("broker down")


async def test_spec024_r8_publish_failure_is_raised_for_retry() -> None:
    """[SPEC-024 R-8] 발행 오류를 삼키지 않아 워커가 재시도 토픽으로 보낸다."""
    service = TrackAnalyzerService(FakeGateway(), _DownPublisher(), clock=lambda: NOW)

    with pytest.raises(PublishError):
        await service.handle(attribute_request("click"), CTX)


_PRIVATE_REPLY = "로그에 남으면 안 되는 답 문장"
_PRIVATE_ASK = "로그에 남으면 안 되는 질문인가요?"


@pytest.mark.parametrize(
    "reply",
    [
        lambda _: {**ANSWER, "answer": _PRIVATE_REPLY},
        lambda _: gateway_error("AI_PROVIDER_UNAVAILABLE", 503),
        lambda _: {"verificationStatus": "confirmed", "answer": _PRIVATE_REPLY},
    ],
)
async def test_spec024_nfr3_logs_have_no_question_or_answer(
    caplog: pytest.LogCaptureFixture, reply: Reply
) -> None:
    caplog.set_level(logging.DEBUG)

    await _run(attribute_request("question", question=_PRIVATE_ASK), FakeGateway(reply=reply))

    for record in caplog.records:
        text = record.getMessage() + repr(record.__dict__)
        assert _PRIVATE_ASK not in text
        assert _PRIVATE_REPLY not in text


async def test_spec024_nfr3_failure_log_has_only_identifiers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="track_analyzer.service")

    await _run(
        attribute_request("question"),
        FakeGateway(reply=lambda _: gateway_error("AI_TIMEOUT", None)),
    )

    [record] = [r for r in caplog.records if r.name == "track_analyzer.service"]
    assert {k: getattr(record, k) for k in ("requestId", "reason", "taskType", "code")} == {
        "requestId": str(attribute_request().request_id),
        "reason": "question",
        "taskType": "object_question",
        "code": "AI_TIMEOUT",
    }
