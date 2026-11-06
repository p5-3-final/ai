"""영상 요약(R-18, R-19). 요약이 없거나 실패해도 나머지 결과는 그대로 낸다.

요약 문장과 이벤트 설명 원문은 로그에 남기지 않는다(NFR-3).
"""

import json
import logging
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from vlm_ai_client.client import AiGatewayError
from vlm_ai_client.models import AnalyzeRequest, AnalyzeResponse
from vlm_contracts.aggregates import Highlight, Summary

from aggregator.compute import Computed
from aggregator.model import AggregateInput
from aggregator.result import empty_summary

SUMMARY_TASK = "source_summary"

logger = logging.getLogger(__name__)


class AnalyzeClient(Protocol):
    """ai-gateway `/v1/analyze`. 운영은 `vlm_ai_client.client.AiGatewayClient`다."""

    async def analyze(
        self, request: AnalyzeRequest, *, traceparent: str | None = None
    ) -> AnalyzeResponse: ...


def summary_variables(source: AggregateInput, computed: Computed) -> dict[str, object]:
    """요약 입력 변수: 타임라인·위험·구역·영상 길이. 이벤트 설명 원문은 넣지 않는다(R-18)."""
    risks = [
        {
            "riskId": str(r.risk_id),
            "zoneId": r.zone_id,
            "riskType": r.risk_type,
            "status": r.status,
            "reason": r.reason,
            "firstSeenMs": r.first_seen_ms,
            "resolvedAtMs": r.resolved_at_ms,
            "carriedOver": r.carried_over,
            "exposure": r.exposure.model_dump(mode="json", by_alias=True),
        }
        for r in computed.risks
    ]
    return {
        "durationSec": source.duration_ms / 1000,
        "zones": [
            {"zoneId": str(z.zone_id), "name": z.name, "kind": z.kind}
            for z in sorted(source.zones, key=lambda z: z.zone_id)
        ],
        "timeline": [json.loads(t.model_dump_json(by_alias=True)) for t in computed.timeline],
        "risks": risks,
    }


async def summarize(
    client: AnalyzeClient,
    source: AggregateInput,
    computed: Computed,
    *,
    traceparent: str | None,
) -> Summary:
    """확정 이벤트나 위험(이어 온 위험 포함)이 있으면 `source_summary`를 한 번 부른다.

    | 조건 | 상태 |
    | --- | --- |
    | 확정 이벤트·위험 없음(부르지 않음) | `empty` |
    | 응답 `modelId` null | `not_configured` |
    | 호출 오류(SPEC-021 R-9 재시도 뒤), 출력 형식 오류, 모델이 있는데 summary null | `failed` |
    | 그 밖 | `generated` |
    """
    if not computed.confirmed and not computed.risks:
        return empty_summary("empty")
    request = AnalyzeRequest(
        task_type=SUMMARY_TASK,
        source_id=source.source_id,
        images=[],
        variables=summary_variables(source, computed),
    )
    try:
        response = await client.analyze(request, traceparent=traceparent)
    except AiGatewayError as e:
        logger.warning("source summary failed", extra={"jobId": str(source.job_id), "code": e.code})
        return empty_summary("failed")
    if response.model_id is None:
        return empty_summary("not_configured")
    try:
        output = _Output.model_validate(response.output)
    except ValidationError:
        output = _Output(summary=None, highlights=[])
    if output.summary is None:
        # 출력 검증에 실패하면 ai-gateway가 summary null 대체 결과를 준다.
        # 문장 없는 generated를 피한다.
        logger.warning("source summary has no text", extra={"jobId": str(source.job_id)})
        return empty_summary("failed")
    return Summary(
        status="generated",
        text=output.summary,
        highlights=output.highlights,
        call_id=response.call_id,
        model_id=response.model_id,
        prompt_version=response.prompt_version,
    )


class _Output(BaseModel):
    """`source_summary` 출력(계약 `_source-summary`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str | None = Field(max_length=1000)
    highlights: list[Highlight] = Field(max_length=5)
