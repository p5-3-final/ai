"""track-analyzer 기동 조립: 설정 검증, ai-gateway 클라이언트, 워커 토픽."""

import time

import httpx
import pytest
from pydantic import ValidationError
from ta_support import ATTRIBUTES, attribute_request
from track_analyzer.__main__ import build_client, build_worker
from track_analyzer.settings import TrackAnalyzerSettings, WorkerSettings
from vlm_ai_client.models import AnalyzeRequest, ImageRef
from vlm_common.health import HealthState


def _settings(**values: object) -> TrackAnalyzerSettings:
    return TrackAnalyzerSettings.model_validate(
        {"ai_gateway_url": "http://ai-gateway:8000"} | values
    )


def test_ai_gateway_url_is_required() -> None:
    with pytest.raises(ValidationError, match="ai_gateway_url"):
        TrackAnalyzerSettings()


@pytest.mark.parametrize("values", [{"max_retries": -1}, {"call_timeout_s": 0}])
def test_spec024_r9_invalid_limits_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


async def test_spec024_r9_client_sends_deadline_from_call_timeout_as_track_analyzer() -> None:
    """[SPEC-024 R-9] 조립한 클라이언트는 설정 제한 시간으로 `X-Request-Deadline`을 싣는다."""
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "callId": "0192f0e1-0000-7000-8000-000000000001",
                "taskType": "track_attribute",
                "modelId": "m",
                "promptVersion": "track_attribute@0123456789ab",
                "taxonomyVersion": "v2",
                "output": ATTRIBUTES,
            },
        )

    client = build_client(_settings(call_timeout_s=30), transport=httpx.MockTransport(gateway))
    started_ms = time.time() * 1000
    async with client:
        message = attribute_request("click")
        await client.analyze(
            AnalyzeRequest(
                task_type="track_attribute",
                source_id=message.source_id,
                images=[ImageRef(storage_key=c.key) for c in message.crops],
                variables={"className": "person"},
            )
        )

    [request] = seen
    assert str(request.url) == "http://ai-gateway:8000/v1/analyze"
    assert request.headers["X-Caller"] == "track-analyzer"
    deadline_ms = int(request.headers["X-Request-Deadline"])
    assert 29_000 <= deadline_ms - started_ms <= 31_000


def test_spec024_r7_r8_worker_uses_retry_and_dlq_topics() -> None:
    settings = _settings()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        settings,
        build_client(settings),
        HealthState(),
    )

    assert worker.required_topics() == [
        "attribute.requested",
        "attribute.requested.track-analyzer.retry",
        "track-analyzer.dlq",
    ]
