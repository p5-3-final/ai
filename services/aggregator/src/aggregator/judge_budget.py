"""job당 VLM 재판정 호출 상한(R-31, SPEC-028 R-8).

활성 위험 중 위험구역 접근 재판정(SPEC-027 R-31)과 재고 비교·장비 방치 판정(SPEC-028)이 같은 계수
`agg:judge-calls:<jobId>`를 쓴다. 실시간 창은 한 세션 안에서 직렬로 처리하므로 계수가 결정적이다.
"""

from uuid import UUID

from aggregator.kv import KeyValueStore

JUDGE_CALLS_TTL_S = 48 * 3600
"""계수 TTL. 실시간 세션 최대 길이(24시간)보다 길게 둔다."""


class JudgeBudget:
    """Redis 계수로 job당 호출 수를 센다."""

    def __init__(self, store: KeyValueStore) -> None:
        self._store = store

    async def try_acquire(self, job_id: UUID, *, limit: int) -> bool:
        """호출 하나를 센다. 센 뒤 값이 `limit` 이하면 True(불러도 됨), 넘으면 False다."""
        if limit <= 0:
            return False
        used = await self._store.incr(f"agg:judge-calls:{job_id}", JUDGE_CALLS_TTL_S)
        return used <= limit
