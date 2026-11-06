"""SPEC-021 NFR-1: 지난 마감은 mock 프로필에서 p95 20ms 안에 504로 끝난다."""

import statistics
import time
from pathlib import Path

from gateway_support import analyze_body, gateway, post_analyze

# 측정 횟수와 기준(spec NFR-1)
_REQUESTS = 200
_P95_LIMIT_S = 0.020


async def test_spec021_nfr1_passed_deadline_p95_under_20ms(registry_dir: Path) -> None:
    latencies: list[float] = []
    async with gateway(registry_dir) as gw:
        for _ in range(_REQUESTS):
            started = time.perf_counter()
            response = await post_analyze(
                gw.client, analyze_body(), headers={"X-Request-Deadline": "1"}
            )
            latencies.append(time.perf_counter() - started)
            assert response.status_code == 504

    p95 = statistics.quantiles(latencies, n=100)[94]
    assert p95 <= _P95_LIMIT_S, f"p95 {p95 * 1000:.1f}ms"
