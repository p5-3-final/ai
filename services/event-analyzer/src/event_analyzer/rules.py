"""규칙 후보의 템플릿 결과(SPEC-023 R-4). I/O 없는 규칙만 둔다.

구역 진입처럼 규칙만으로 분명한 사건은 VLM을 부르지 않고 프로필 문장으로 바로 확정한다.
"""

from vlm_contracts.events import DetectorCandidate, VLMAnalysis

from event_analyzer.profile import EventAnalyzerProfile

_ZONE_PLACEHOLDER = "{zoneName}"


def rule_analysis(candidate: DetectorCandidate, profile: EventAnalyzerProfile) -> VLMAnalysis:
    """규칙 후보 하나의 확정 결과. 판정 필드 중 규칙이 정하지 않는 값은 null이다."""
    rule = profile.rules[candidate.event_type]
    zone = candidate.zone_name or profile.rule_text.missing_zone_name
    # str.format은 다른 중괄호를 해석하므로 자리 표시 하나만 바꾼다.
    description = rule.template.replace(_ZONE_PLACEHOLDER, zone)
    return VLMAnalysis.unknown(candidate.event_type).model_copy(
        update={
            "event_detected": True,
            "verification_status": "confirmed",
            "description": description,
            "severity": rule.severity,
            "evidence_start": candidate.start_ms / 1000,
            "evidence_end": candidate.end_ms / 1000,
        }
    )
