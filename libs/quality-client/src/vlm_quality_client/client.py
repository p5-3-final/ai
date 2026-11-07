"""quality-service 내부 클라이언트(계약 `openapi/quality-service.yaml`, SPEC-030 R-19)."""

from vlm_common.http_call import Deadline

from vlm_quality_client.models import StageGateRequest, StageGateResult

__all__ = ["QualityApiClient"]


class QualityApiClient:
    """quality-service 내부 클라이언트."""

    async def evaluate_stage_gate(
        self, request: StageGateRequest, *, deadline: Deadline
    ) -> StageGateResult:
        """처리 단계 성능 Gate(`evaluateStageGate`)."""
        raise NotImplementedError
