"""event-analyzer 기동 조립: 설정 검증, ai-gateway 클라이언트, 워커 토픽."""

import time
from pathlib import Path

import httpx
import pytest
from ea_support import PROFILE_PATH, candidate, candidates_message, judged
from event_analyzer.__main__ import Dependencies, build_client, build_worker, load_dependencies
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rejudge_state import NoRejudgeState, RedisRejudgeState
from event_analyzer.settings import EventAnalyzerSettings, WorkerSettings
from pydantic import ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, ImageRef
from vlm_common.health import HealthState
from vlm_common.profile import ProfileError, load_profile
from vlm_storage.memory import InMemoryStorage


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
                images=[ImageRef(storage_key=k) for k in source.candidates[0].frame_keys],
                variables={},
            )
        )

    [request] = seen
    assert str(request.url) == "http://ai-gateway:8000/v1/analyze"
    assert request.headers["X-Caller"] == "event-analyzer"


def _dependencies() -> Dependencies:
    return Dependencies(
        storage=InMemoryStorage(),
        profile=load_profile(PROFILE_PATH, EventAnalyzerProfile),
        rejudge=NoRejudgeState(),
    )


def test_spec020_r16_r17_spec023_r14_r15_worker_uses_retry_and_dlq_topics() -> None:
    settings = _settings()
    worker = build_worker(
        WorkerSettings(kafka_bootstrap="localhost:9092"),
        settings,
        build_client(settings),
        HealthState(),
        _dependencies(),
    )

    assert worker.required_topics() == [
        "event.candidate.vector",
        "event.candidate",
        "alert.candidate",
        "event.candidate.vector.event-analyzer.retry",
        "event.candidate.event-analyzer.retry",
        "alert.candidate.event-analyzer.retry",
        "event-analyzer.dlq",
    ]


def test_spec023_r16_invalid_profile_fails_startup(tmp_path: Path) -> None:
    broken = tmp_path / "broken.toml"
    broken.write_text("[merge]\ncooldown_s = 0\n", encoding="utf-8")

    with pytest.raises(ProfileError):
        load_dependencies(_settings(profile_path=broken), storage=InMemoryStorage())


def test_spec023_r16_missing_profile_fails_startup(tmp_path: Path) -> None:
    with pytest.raises(ProfileError):
        load_dependencies(
            _settings(profile_path=tmp_path / "missing.toml"), storage=InMemoryStorage()
        )


def test_spec023_r18_redis_state_is_used_only_when_configured() -> None:
    plain = load_dependencies(_settings(profile_path=PROFILE_PATH), storage=InMemoryStorage())
    with_redis = load_dependencies(
        _settings(profile_path=PROFILE_PATH, redis_url="redis://redis:6379/0"),
        storage=InMemoryStorage(),
    )

    assert isinstance(plain.rejudge, NoRejudgeState)
    assert isinstance(with_redis.rejudge, RedisRejudgeState)


async def test_spec021_r9_r14_client_retries_only_connection_failures_within_call_timeout() -> None:
    """[SPEC-021 R-9·R-14] 조립한 클라이언트는 연결 실패만 다시 부르고 503·504는 한 번이다.

    마감은 호출 제한 시간(`call_timeout_s`)이다.
    """
    seen: list[httpx.Request] = []
    replies: list[httpx.Response | Exception] = [
        httpx.ConnectError("refused"),
        httpx.Response(
            504,
            json={
                "type": "about:blank",
                "title": "t",
                "status": 504,
                "code": "AI_DEADLINE_EXCEEDED",
            },
        ),
    ]

    def gateway(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    settings = _settings(call_timeout_s=30, max_retries=2, retry_base_delay_s=0)
    client = build_client(settings, transport=httpx.MockTransport(gateway))
    started_ms = time.time() * 1000
    async with client:
        source = candidates_message(candidate(0))
        with pytest.raises(AiGatewayError) as raised:
            await client.analyze(
                AnalyzeRequest(
                    task_type="event_judge",
                    source_id=source.source_id,
                    images=[ImageRef(storage_key=k) for k in source.candidates[0].frame_keys],
                    variables={},
                )
            )

    assert raised.value.code == "AI_DEADLINE_EXCEEDED"
    assert len(seen) == 2
    deadline_ms = int(seen[0].headers["X-Request-Deadline"])
    assert 29_000 <= deadline_ms - started_ms <= 31_000
