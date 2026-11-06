"""판정 결과와 재판정·최종 판정 규칙(SPEC-020 R-3~R-7, R-12, R-13).

I/O 없는 규칙만 둔다. 호출과 발행은 `service`가 한다.
"""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from vlm_contracts.events import DetectableEventType, Judgment, VLMAnalysis

type Stage = Literal["judge", "escalate"]
type Method = Literal["vector+vlm", "vector+vlm+escalate"]
type ReviewReason = Literal["judgment_disagreement"]

_TASK_TYPES: dict[Stage, Literal["event_judge", "event_judge_escalate"]] = {
    "judge": "event_judge",
    "escalate": "event_judge_escalate",
}
# 판정이 정해진 상태. 나머지(unverified·unknown)는 모델이 판단하지 못한 것이다.
_DECIDED = frozenset({"confirmed", "rejected"})


@dataclass(frozen=True, slots=True)
class Outcome:
    """판정 호출 하나의 결과. ai-gateway가 답했거나(`called`) 끝내 실패했다(`failed`)."""

    stage: Stage
    analysis: VLMAnalysis
    call_id: UUID | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    # None이 아니면 호출이 실패했고 `analysis`는 event-analyzer가 만든 unknown이다.
    error_code: str | None = None

    @classmethod
    def called(
        cls,
        stage: Stage,
        call_id: UUID,
        model_id: str | None,
        prompt_version: str,
        analysis: VLMAnalysis,
    ) -> "Outcome":
        """ai-gateway가 돌려준 결과. 모델 미설정·예산 초과면 `model_id`가 None이다."""
        return cls(stage, analysis, call_id, model_id, prompt_version)

    @classmethod
    def failed(cls, stage: Stage, event_type: DetectableEventType, error_code: str) -> "Outcome":
        """호출이 결과 없이 끝났다. 후보를 버리지 않고 `unknown`으로 남긴다(R-12)."""
        return cls(stage, VLMAnalysis.unknown(event_type), error_code=error_code)

    def record(self) -> Judgment:
        """`event.analyzed`의 `judgments` 항목."""
        return Judgment(
            stage=self.stage,
            task_type=_TASK_TYPES[self.stage],
            call_id=self.call_id,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            verification_status=self.analysis.verification_status,
            error_code=self.error_code,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """후보 하나의 최종 판정."""

    analysis: VLMAnalysis
    method: Method
    judgments: list[Judgment]
    review_required: bool
    review_reasons: list[ReviewReason]


def needs_escalation(first: Outcome) -> bool:
    """1차 판정을 더 강한 모델로 다시 볼지(R-3, R-4).

    호출 실패나 모델 미설정은 다시 불러도 같은 이유로 판정하지 못하므로 올리지 않는다.
    """
    if first.error_code is not None or first.analysis.verification_status == "unverified":
        return False
    return first.analysis.severity == "high" or first.analysis.event_detected is None


def decide(first: Outcome, escalated: Outcome | None) -> Decision:
    """1차와 재판정에서 최종 분석·판정 방식·검토 표시를 정한다(R-5~R-7, R-13)."""
    if escalated is None:
        return Decision(first.analysis, "vector+vlm", [first.record()], False, [])
    statuses = {first.analysis.verification_status, escalated.analysis.verification_status}
    final = (
        escalated.analysis if escalated.analysis.verification_status in _DECIDED else first.analysis
    )
    # 두 모델이 정반대로 판정했으면 어느 쪽도 믿기 어려워 사람이 본다(R-7).
    disagreement = statuses == _DECIDED
    return Decision(
        final,
        "vector+vlm+escalate",
        [first.record(), escalated.record()],
        disagreement,
        ["judgment_disagreement"] if disagreement else [],
    )
