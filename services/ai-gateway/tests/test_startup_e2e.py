"""운영 조립(build_production)을 실제 PostgreSQL·Kafka·SeaweedFS로 검증한다."""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from ai_gateway.db.migrate import upgrade
from ai_gateway.main import production_app
from gateway_support import CALLER, IMAGE_KEYS, analyze_body
from vlm_common.profile import ProfileError
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_testing import containers

pytestmark = pytest.mark.integration

BUCKET = "vlm-media"
GROUP = "ai-gateway"
PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "local-mock.toml"


@pytest.fixture
def environment(
    monkeypatch: pytest.MonkeyPatch, database_url: str, contracts_dir: Path
) -> Iterator[None]:
    with containers.kafka() as bootstrap, containers.seaweedfs() as endpoint:
        containers.create_topics(
            bootstrap, ["video.deleted", f"video.deleted.{GROUP}.retry", f"{GROUP}.dlq"]
        )
        containers.create_bucket(endpoint, BUCKET)
        for name, value in {
            "AI_GATEWAY_DATABASE_URL": database_url,
            "AI_GATEWAY_PROFILE_PATH": str(PROFILE),
            "AI_GATEWAY_CONTRACTS_DIR": str(contracts_dir),
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
