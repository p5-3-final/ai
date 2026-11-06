"""호출 로그(`ai.ai_call_log`).

호출자별 사용량·모델·프롬프트 버전·실패 원인을 남긴다(SPEC-009 R-17).
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import insert, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_gateway.db.tables import call_log

type JsonObject = dict[str, object]
type CallStatus = Literal["success", "unavailable", "failed", "invalid_output", "budget_exceeded"]


def new_call_id() -> UUID:
    """요청마다 새 호출 ID. 여러 Pod가 동시에 만들어도 겹치지 않는다(NFR-6)."""
    return uuid4()


@dataclass(frozen=True, slots=True)
class CallLogEntry:
    """호출 한 건. 모델 미설정이면 `adapter`·`model_id`가 없다(R-8).

    임베딩 호출은 영상 ID·프롬프트 버전이 없고 입력 수를 남긴다(SPEC-011 R-23).
    """

    call_id: UUID
    caller: str
    source_id: UUID | None
    task_type: str
    adapter: str | None
    model_id: str | None
    prompt_version: str | None
    # 임베딩 호출과 분류 체계를 참조하지 않는 taskType은 없다(SPEC-009 R-29).
    taxonomy_version: str | None
    status: CallStatus
    error_code: str | None
    latency_ms: int
    image_keys: list[str]
    request_config: JsonObject
    output: JsonObject | None
    raw_output: str | None
    input_tokens: int | None
    output_tokens: int | None
    trace_id: str | None
    input_count: int | None = None
    # 캐시·예산 대상 호출만 채운다(SPEC-016 R-19).
    cache_hit: bool | None = None
    reserved_cost_usd: Decimal | None = None
    settled_cost_usd: Decimal | None = None


class CallLog:
    """호출 로그 저장소."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def save(self, entry: CallLogEntry) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(call_log).values(**asdict(entry)))

    async def clear_raw_output(self, source_id: UUID) -> int:
        """영상의 모델 원문을 지운다(R-20). 지운 행 수를 돌려준다."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(call_log)
                .where(call_log.c.source_id == source_id, call_log.c.raw_output.is_not(None))
                .values(raw_output=None)
            )
        return result.rowcount
