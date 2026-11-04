"""운영 조립(build_production)을 실제 PostgreSQL·Kafka·SeaweedFS로 검증한다."""

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
from ai_gateway.db.migrate import upgrade
from ai_gateway.main import production_app
from fastapi import FastAPI
from gateway_support import CALLER, IMAGE_KEYS, analyze_body, random_weights
from vlm_common.profile import ProfileError
from vlm_encoders.base import EncoderLoadError
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = pytest.mark.integration

BUCKET = "vlm-media"
GROUP = "ai-gateway"
PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "local-mock.toml"


@pytest.fixture
def environment(
    monkeypatch: pytest.MonkeyPatch, database_url: str, registry_dir: Path
) -> Iterator[None]:
    with containers.kafka() as bootstrap, containers.seaweedfs() as endpoint:
        containers.create_topics(
            bootstrap, ["video.deleted", f"video.deleted.{GROUP}.retry", f"{GROUP}.dlq"]
        )
        containers.create_bucket(endpoint, BUCKET)
        for name, value in {
            "AI_GATEWAY_DATABASE_URL": database_url,
            "AI_GATEWAY_PROFILE_PATH": str(PROFILE),
            "AI_GATEWAY_REGISTRY_DIR": str(registry_dir),
            "AI_GATEWAY_ALLOW_MOCK": "true",
            "KAFKA_BOOTSTRAP": bootstrap,
            "S3_BUCKET": BUCKET,
            "S3_ENDPOINT_URL": endpoint,
            "S3_PATH_STYLE": "true",
            "AWS_ACCESS_KEY_ID": containers.TEST_ACCESS_KEY,
            "AWS_SECRET_ACCESS_KEY": containers.TEST_ACCESS_KEY,
        }.items():
            monkeypatch.setenv(name, value)
        storage = S3Storage(StorageSettings())
        for key in IMAGE_KEYS:
            storage.put_bytes(key, b"jpeg", "image/jpeg")
        yield


@pytest.mark.usefixtures("environment")
async def test_spec009_r14_r23_production_app_starts_and_analyzes(database_url: str) -> None:
    await upgrade(database_url)
    app = production_app()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-gateway") as client:
            ready = 503
            for _ in range(40):
                ready = (await client.get("/ready")).status_code
                if ready == 200:
                    break
                await asyncio.sleep(0.25)
            assert ready == 200
            response = await client.post(
                "/v1/analyze", json=analyze_body(), headers={"X-Caller": CALLER}
            )

    assert response.status_code == 200
    assert response.json()["output"]["verificationStatus"] == "confirmed"
    assert response.json()["modelId"] == "mock"


@pytest.mark.usefixtures("environment")
async def test_spec009_r15_production_app_refuses_mock_without_allow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_GATEWAY_ALLOW_MOCK", "false")
    app = production_app()

    with pytest.raises(ProfileError, match="mock"):
        async with app.router.lifespan_context(app):
            pass


MODELS_BUCKET = "vlm-models"
WEIGHTS_KEY = "open_clip/MobileCLIP2-S0/dfndr2b/open_clip_model.safetensors"


@pytest.fixture
def open_clip_profile(environment: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """local-mock 프로필의 인코더만 open_clip으로 바꾸고, 무작위 가중치를 모델 버킷에 올린다."""
    data = random_weights()
    endpoint = StorageSettings(bucket=MODELS_BUCKET).endpoint_url
    assert endpoint is not None
    containers.create_bucket(endpoint, MODELS_BUCKET)
    S3Storage(StorageSettings(bucket=MODELS_BUCKET)).put_bytes(
        WEIGHTS_KEY, data, "application/octet-stream"
    )
    text = PROFILE.read_text(encoding="utf-8")
    start = text.index("[encoder]")
    end = text.index("\n[", start + 1)
    encoder = (
        '[encoder]\nkind = "open_clip"\n'
        'model_id = "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"\ndimension = 512\n'
        f'weights_key = "{WEIGHTS_KEY}"\nweights_sha256 = "{hashlib.sha256(data).hexdigest()}"\n'
    )
    path = tmp_path / "open-clip.toml"
    path.write_text(text[:start] + encoder + text[end:], encoding="utf-8")
    monkeypatch.setenv("AI_GATEWAY_PROFILE_PATH", str(path))
    monkeypatch.setenv("AI_GATEWAY_MODELS_BUCKET", MODELS_BUCKET)
    monkeypatch.setenv("AI_GATEWAY_MODEL_CACHE_DIR", str(tmp_path / "cache"))


async def _ready_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ai-gateway") as client:
        for _ in range(40):
            if (await client.get("/ready")).status_code == 200:
                break
            await asyncio.sleep(0.25)
        yield client


@pytest.mark.usefixtures("environment")
async def test_spec011_r1_r3_local_mock_profile_embeds(database_url: str) -> None:
    """[SPEC-011 R-1, R-3] local-mock 프로필로 운영 조립을 띄워 시각·텍스트 벡터를 만든다."""
    await upgrade(database_url)
    app = production_app()

    async with app.router.lifespan_context(app):
        async for client in _ready_client(app):
            visual = await client.post(
                "/v1/embed",
                json={"taskType": "embed_visual_text", "texts": ["a forklift"]},
                headers={"X-Caller": "search-service"},
            )
            text = await client.post(
                "/v1/embed",
                json={"taskType": "embed_text", "texts": ["작업자가 넘어졌다"]},
                headers={"X-Caller": "embedding-worker"},
            )

    assert visual.status_code == 200, visual.text
    assert text.status_code == 200, text.text
    assert len(text.json()["vectors"][0]) == text.json()["dimension"]


@pytest.mark.usefixtures("open_clip_profile")
async def test_spec011_r15_r25_downloads_weights_then_becomes_ready(database_url: str) -> None:
    """[SPEC-011 R-15, R-25] 모델 버킷의 가중치를 받아 로드한 뒤 준비되고 벡터를 만든다."""
    await upgrade(database_url)
    app = production_app()

    async with app.router.lifespan_context(app):
        async for client in _ready_client(app):
            ready = (await client.get("/ready")).status_code
            response = await client.post(
                "/v1/embed",
                json={"taskType": "embed_visual_text", "texts": ["a forklift"]},
                headers={"X-Caller": "search-service"},
            )

    assert ready == 200
    assert response.status_code == 200, response.text
    assert response.json()["modelId"] == "open_clip:MobileCLIP2-S0:dfndr2b:mean-4"
    assert len(response.json()["vectors"][0]) == 512


@pytest.mark.usefixtures("open_clip_profile")
async def test_spec011_r16_tampered_weights_fail_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[SPEC-011 R-16] 모델 버킷의 가중치가 프로필 해시와 다르면 앱이 뜨지 않는다."""
    S3Storage(StorageSettings(bucket=MODELS_BUCKET)).put_bytes(
        WEIGHTS_KEY, b"tampered", "application/octet-stream"
    )
    app = production_app()

    with pytest.raises(EncoderLoadError, match="sha256"):
        async with app.router.lifespan_context(app):
            pass
