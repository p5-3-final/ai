"""event-analyzer 기동 조립: 설정 검증, ai-gateway 클라이언트, 워커 토픽."""

import httpx
import pytest
from ea_support import candidate, candidates_message, judged
from event_analyzer.__main__ import build_client, build_worker
from event_analyzer.settings import EventAnalyzerSettings, WorkerSettings
from pydantic import ValidationError
from vlm_ai_client.client import AnalyzeImage, AnalyzeRequest
from vlm_common.health import HealthState


def _settings(**values: object) -> EventAnalyzerSettings:
    return EventAnalyzerSettings.model_validate(
        {"ai_gateway_url": "http://ai-gateway:8000"} | values
    )


def test_ai_gateway_url_is_required() -> None:
    with pytest.raises(ValidationError, match="ai_gateway_url"):
        EventAnalyzerSettings()


@pytest.mark.parametrize("values", [{"concurrency": 0}, {"max_retries": -1}, {"call_timeout_s": 0}])
def test_spec020_r11_r18_invalid_limits_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


async def test_spec020_r1_client_calls_configured_gateway_as_event_analyzer() -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "callId": "0192f0e1-0000-7000-8000-000000000001",
                "taskType": "event_judge",
                "modelId": "m",
                "promptVersion": "event_judge@0123456789ab",
                "taxonomyVersion": "v2",
                "output": judged("fall"),
            },
        )

    client = build_client(_settings(), transport=httpx.MockTransport(gateway))
    async with client:
        source = candidates_message(candidate(0))
        await client.analyze(
            AnalyzeRequest(
                task_type="event_judge",
                source_id=source.source_id,
                images=[AnalyzeImage(storage_key=k) for k in source.candidates[0].frame_keys],
                variables={},
            )
        )

    [request] = seen
    assert str(request.url) == "http://ai-gateway:8000/v1/analyze"
    assert request.headers["X-Caller"] == "event-analyzer"


def test_spec020_r16_r17_worker_uses_retry_and_dlq_topics() -> None:
    settings = _settings()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        settings,
        build_client(settings),
        HealthState(),
    )

    assert worker.required_topics() == [
        "event.candidate.vector",
        "event.candidate.vector.event-analyzer.retry",
        "event-analyzer.dlq",
    ]
