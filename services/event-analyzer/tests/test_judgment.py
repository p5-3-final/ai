"""재판정 조건과 최종 판정 규칙(SPEC-020 R-3~R-7)."""

from uuid import UUID

import pytest
from event_analyzer.judgment import Outcome, decide, needs_escalation
from vlm_contracts.events import Severity, VerificationStatus, VLMAnalysis

CALL = UUID("0192f0e1-0000-7000-8000-000000000001")


def _analysis(
    status: VerificationStatus = "confirmed",
    *,
    severity: Severity | None = "medium",
    detected: bool | None = True,
) -> VLMAnalysis:
    return VLMAnalysis.unknown("fall").model_copy(
        update={
            "verification_status": status,
            "severity": severity,
            "event_detected": detected,
            "description": None if status in {"unknown", "unverified"} else "작업자가 넘어졌다.",
        }
    )


def _judge(analysis: VLMAnalysis) -> Outcome:
    return Outcome.called("judge", CALL, "gpt-6.1-terra", "event_judge@0123456789ab", analysis)


def _escalate(analysis: VLMAnalysis) -> Outcome:
    return Outcome.called(
        "escalate", CALL, "gpt-6.1-sol", "event_judge_escalate@0123456789ab", analysis
    )


def _failed_judge() -> Outcome:
    return Outcome.failed("judge", "fall", "AI_TIMEOUT")


@pytest.mark.parametrize(
    "analysis",
    [
        _analysis(severity="high"),
        _analysis("rejected", severity="high", detected=False),
        _analysis("unknown", severity=None, detected=None),
    ],
)
def test_spec020_r3_high_or_undecided_is_escalated(analysis: VLMAnalysis) -> None:
    assert needs_escalation(_judge(analysis))


@pytest.mark.parametrize(
    "analysis",
    [
        _analysis(severity="medium"),
        _analysis(severity="low"),
        _analysis("rejected", detected=False),
    ],
)
def test_spec020_r3_decided_medium_or_low_is_not_escalated(analysis: VLMAnalysis) -> None:
    assert not needs_escalation(_judge(analysis))


def test_spec020_r4_unverified_is_not_escalated() -> None:
    unverified = _analysis("unverified", severity=None, detected=None)

    assert not needs_escalation(_judge(unverified))


def test_spec020_r4_failed_call_is_not_escalated() -> None:
    assert not needs_escalation(_failed_judge())


def test_spec020_r12_failed_call_is_unknown_with_candidate_type() -> None:
    failed = Outcome.failed("judge", "ppe_missing", "AI_IMAGE_NOT_FOUND")

    assert failed.analysis == VLMAnalysis.unknown("ppe_missing")
    record = failed.record()
    assert (record.call_id, record.model_id, record.prompt_version) == (None, None, None)
    assert (record.verification_status, record.error_code) == ("unknown", "AI_IMAGE_NOT_FOUND")
    assert record.task_type == "event_judge"


def test_spec020_r6_single_judgment_is_vector_vlm() -> None:
    first = _judge(_analysis())

    decision = decide(first, None)

    assert decision.method == "vector+vlm"
    assert decision.analysis == first.analysis
    assert [j.stage for j in decision.judgments] == ["judge"]
    assert (decision.review_required, decision.review_reasons) == (False, [])


@pytest.mark.parametrize("status", ["confirmed", "rejected"])
def test_spec020_r5_decided_escalation_is_final(status: VerificationStatus) -> None:
    first = _judge(_analysis(severity="high"))
    second = _escalate(_analysis(status, severity="high", detected=status == "confirmed"))

    decision = decide(first, second)

    assert decision.method == "vector+vlm+escalate"
    assert decision.analysis == second.analysis
    assert [(j.stage, j.task_type) for j in decision.judgments] == [
        ("judge", "event_judge"),
        ("escalate", "event_judge_escalate"),
    ]


@pytest.mark.parametrize("status", ["unknown", "unverified"])
def test_spec020_r5_undecided_escalation_keeps_first(status: VerificationStatus) -> None:
    first = _judge(_analysis(severity="high"))
    second = _escalate(_analysis(status, severity=None, detected=None))

    decision = decide(first, second)

    assert decision.analysis == first.analysis
    assert decision.method == "vector+vlm+escalate"


def test_spec020_r13_failed_escalation_keeps_first_and_records_error() -> None:
    first = _judge(_analysis(severity="high"))

    decision = decide(first, Outcome.failed("escalate", "fall", "AI_PROVIDER_UNAVAILABLE"))

    assert decision.analysis == first.analysis
    assert decision.method == "vector+vlm+escalate"
    escalated = decision.judgments[1]
    assert (escalated.task_type, escalated.error_code) == (
        "event_judge_escalate",
        "AI_PROVIDER_UNAVAILABLE",
    )
    assert escalated.verification_status == "unknown"


@pytest.mark.parametrize(
    ("first", "second"),
    [("confirmed", "rejected"), ("rejected", "confirmed")],
)
def test_spec020_r7_disagreement_requires_review(
    first: VerificationStatus, second: VerificationStatus
) -> None:
    decision = decide(
        _judge(_analysis(first, severity="high", detected=first == "confirmed")),
        _escalate(_analysis(second, severity="high", detected=second == "confirmed")),
    )

    assert (decision.review_required, decision.review_reasons) == (True, ["judgment_disagreement"])


@pytest.mark.parametrize(
    ("first", "second"),
    [("confirmed", "confirmed"), ("unknown", "confirmed"), ("confirmed", "unknown")],
)
def test_spec020_r7_agreement_or_undecided_needs_no_review(
    first: VerificationStatus, second: VerificationStatus
) -> None:
    decision = decide(
        _judge(_analysis(first, severity="high", detected=None if first == "unknown" else True)),
        _escalate(_analysis(second, severity=None, detected=None if second == "unknown" else True)),
    )

    assert (decision.review_required, decision.review_reasons) == (False, [])
