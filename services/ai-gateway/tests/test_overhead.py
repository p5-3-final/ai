"""SPEC-009 NFR-1: 공급자 호출을 뺀 자체 처리 시간."""

import statistics
import time
from pathlib import Path

import pytest
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.call_log import CallLog
from ai_gateway.db.migrate import upgrade
from gateway_support import SOURCE, analyze_body, gateway, post_analyze, profile
from sqlalchemy.ext.asyncio import create_async_engine
from vlm_storage.memory import InMemoryStorage

pytestmark = pytest.mark.integration

IMAGES = [f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/{i:03d}.jpg" for i in range(10)]
# 블러 사본 대표 프레임 한 장 크기(대략 1280x720 JPEG)
IMAGE_BYTES = 150_000
REQUESTS = 50


def _put_images(storage: InMemoryStorage) -> None:
    for key in IMAGES:
        storage.put_bytes(key, b"\xff" * IMAGE_BYTES, "image/jpeg")


async def test_spec009_nfr1_overhead_p95_under_300ms(
    contracts_dir: Path, database_url: str
) -> None:
    await upgrade(database_url)
    engine = create_async_engine(database_url)
    body = analyze_body(images=[{"storageKey": k, "ptsSec": None} for k in IMAGES])
    timings: list[float] = []

    async with gateway(
        contracts_dir,
        profile(max_images=10),
        prepare=_put_images,
        adapters={"fake": MockAdapter()},
        call_log=CallLog(engine),
    ) as gw:
        for _ in range(REQUESTS):
            started = time.perf_counter()
            response = await post_analyze(gw.client, body)
            timings.append(time.perf_counter() - started)
            assert response.status_code == 200
    await engine.dispose()

    p95 = statistics.quantiles(timings, n=20)[-1]
    assert p95 <= 0.3, f"p95 {p95 * 1000:.0f}ms"
