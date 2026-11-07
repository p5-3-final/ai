"""embedding-worker 기동 조립: 설정 검증, ai-gateway 클라이언트, 워커 토픽.

SPEC-025 R-5, R-9, R-11.
"""

import time

import httpx
import pytest
from embedding_worker.__main__ import build_client, build_worker
from embedding_worker.settings import EmbeddingWorkerSettings, WorkerSettings
from ew_support import DIMENSION, MODEL, unit_vector
from pydantic import ValidationError
from vlm_ai_client.models import EmbedRequest
from vlm_common.health import HealthState
from vlm_storage.memory import InMemoryStorage


def _settings(**values: object) -> EmbeddingWorkerSettings:
    return EmbeddingWorkerSettings.model_validate(
        {"ai_gateway_url": "http://ai-gateway:8000"} | values
    )


def test_ai_gateway_url_is_required() -> None:
    with pytest.raises(ValidationError, match="ai_gateway_url"):
        EmbeddingWorkerSettings()


def test_spec025_r5_max_chars_defaults_to_2000() -> None:
    assert _settings().max_chars == 2000


@pytest.mark.parametrize(
    "values",
    [
        {"max_chars": 2001},
        {"max_chars": 0},
        {"max_retries": -1},
        {"call_timeout_s": 0},
        {"track_mapping_cache": 0},
    ],
)
def test_invalid_limits_fail_startup(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**values)


async def test_spec025_r6_client_calls_embed_as_embedding_worker_with_deadline() -> None:
    seen: list[httpx.Request] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "callId": "0192f0e1-0000-7000-8000-000000000001",
                "taskType": "embed_text",
                "modelId": MODEL,
                "dimension": DIMENSION,
                "vectors": [unit_vector()],
            },
        )

    client = build_client(_settings(call_timeout_s=20), transport=httpx.MockTransport(gateway))
    started_ms = time.time() * 1000
    async with client:
        await client.embed(EmbedRequest(task_type="embed_text", texts=["문장"]))

    [request] = seen
    assert str(request.url) == "http://ai-gateway:8000/v1/embed"
    assert request.headers["X-Caller"] == "embedding-worker"
    deadline_ms = int(request.headers["X-Request-Deadline"])
    assert 19_000 <= deadline_ms - started_ms <= 21_000


def test_spec025_r9_r11_worker_consumes_three_topics_with_retry_and_dlq() -> None:
    settings = _settings()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        settings,
        build_client(settings),
        InMemoryStorage(),
        HealthState(),
    )

    assert worker.required_topics() == [
        "event.analyzed",
        "track.analyzed",
        "segment.annotated",
        "event.analyzed.embedding-worker.retry",
        "track.analyzed.embedding-worker.retry",
        "segment.annotated.embedding-worker.retry",
        "embedding-worker.dlq",
    ]
