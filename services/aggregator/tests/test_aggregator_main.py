"""aggregator 기동 조립: 설정 검증, 클라이언트, 워커 경로(SPEC-027 R-1, R-25, R-29, R-33)."""

from uuid import uuid4

import httpx
import pytest
from aggregator.__main__ import Transports, build_clients, build_worker
from aggregator.settings import AggregatorSettings, WorkerSettings
from pydantic import ValidationError
from vlm_common.health import HealthState
from vlm_common.http_call import Deadline
from vlm_storage.memory import InMemoryStorage

_URLS = {
    "ai_gateway_url": "http://ai-gateway:8000",
    "analysis_api_url": "http://analysis-api:8085",
}


def _settings(**values: object) -> AggregatorSettings:
    return AggregatorSettings.model_validate(_URLS | values)


def test_spec027_r3_internal_api_urls_are_required() -> None:
    with pytest.raises(ValidationError, match="analysis_api_url"):
        AggregatorSettings.model_validate({"ai_gateway_url": "http://x"})


@pytest.mark.parametrize(
    "values", [{"read_timeout_s": 0}, {"page_limit": 1001}, {"max_attempts": 0}]
)
def test_spec027_r3_invalid_limits_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


def test_spec027_r3_default_read_deadline_is_300_seconds() -> None:
    assert (_settings().read_timeout_s, _settings().page_limit) == (300, 1000)


async def test_spec027_r3_analysis_client_calls_as_aggregator() -> None:
    seen: list[httpx.Request] = []

    def api(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    clients = build_clients(_settings(), Transports(analysis=httpx.MockTransport(api)))
    async with clients.analysis:
        await clients.analysis.list_source_zones(uuid4(), deadline=Deadline.start(5))
    await clients.aclose()

    assert seen[0].headers["X-Caller"] == "aggregator"
    assert str(seen[0].url).startswith("http://analysis-api:8085/internal/v1/")


def test_spec027_r1_worker_subscribes_to_all_aggregator_topics() -> None:
    """[SPEC-027 R-1·R-25·R-29] analysis.completed·event.analyzed·track.detected·chunk.manifest."""
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        _settings(),
        InMemoryStorage(),
        build_clients(_settings(), Transports()),
        HealthState(),
    )

    topics = set(worker.required_topics())

    for topic in ("analysis.completed", "event.analyzed", "track.detected", "chunk.manifest"):
        assert {topic, f"{topic}.aggregator.retry"} <= topics
    assert "aggregator.dlq" in topics
