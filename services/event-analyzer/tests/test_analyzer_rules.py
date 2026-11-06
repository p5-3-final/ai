"""규칙 후보의 템플릿 결과(SPEC-023 R-4, US-2)."""

from pathlib import Path

from ea_support import detected
from event_analyzer.profile import EventAnalyzerProfile
from event_analyzer.rules import rule_analysis
from vlm_common.profile import load_profile

PROFILE = load_profile(
    Path(__file__).resolve().parents[1] / "profiles" / "local.toml", EventAnalyzerProfile
)


def test_spec023_r4_us2_rule_candidate_is_confirmed_with_profile_template() -> None:
    zone = "A동 크레인 하부"
    candidate = detected(
        1, "hazard_zone_access", start_s=52, end_s=53.5, judgment="rule", zone_name=zone
    )

    analysis = rule_analysis(candidate, PROFILE)

    assert analysis.model_dump(by_alias=True) == {
        "eventType": "hazard_zone_access",
        "eventDetected": True,
        "verificationStatus": "confirmed",
        "description": "A동 크레인 하부 구역에 사람이 들어왔다.",
        "beforeState": None,
        "afterState": None,
        "riskDetected": None,
        "riskType": None,
        "riskReason": None,
        "solution": None,
        "severity": PROFILE.rules["hazard_zone_access"].severity,
        "evidenceStart": 52.0,
        "evidenceEnd": 53.5,
    }


def test_spec023_r4_d9_missing_zone_name_uses_profile_placeholder() -> None:
    candidate = detected(1, "hazard_zone_access", judgment="rule", zone_name=None)

    analysis = rule_analysis(candidate, PROFILE)

    assert analysis.description == (
        f"{PROFILE.rule_text.missing_zone_name} 구역에 사람이 들어왔다."
    )


def test_spec023_r4_other_braces_in_template_stay_literal() -> None:
    rules = {**PROFILE.rules}
    rules["fall"] = rules["fall"].model_copy(update={"template": "{zoneName} {x} {{y}}"})
    profile = PROFILE.model_copy(update={"rules": rules})

    analysis = rule_analysis(detected(1, "fall", judgment="rule", zone_name="B"), profile)

    assert analysis.description == "B {x} {{y}}"
