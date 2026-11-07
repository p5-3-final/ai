"""event-analyzer 프로필(TOML). 판정 규칙값은 코드가 아니라 이 값으로 바꾼다(SPEC-023 R-16).

틀린 프로필은 기동에 실패한다. 판정 중에 빠진 템플릿을 만나 후보를 버리기보다 멈추는 편이 안전하다.
"""

from typing import Annotated, Self, get_args

from pydantic import Field, StringConstraints, model_validator
from vlm_common.profile import Profile
from vlm_contracts.events import DetectableEventType, Severity

type _Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
_DETECTABLE: frozenset[str] = frozenset(get_args(DetectableEventType.__value__))


class MergeProfile(Profile):
    """쿨다운 병합(`[merge]`, SPEC-023 R-3)."""

    # 같은 트랙·유형의 앞 후보 끝과 다음 후보 시작 간격이 이 값 이하면 하나로 합친다.
    cooldown_s: float = Field(default=10, gt=0)


class RuleTextProfile(Profile):
    """규칙 후보 문장에 공통으로 쓰는 값(`[rule_text]`)."""

    # SPEC-023 D-9: 구역 이름이 null인 후보의 템플릿에 넣는 글자. 구역 이름 상한(100자)과 같다.
    missing_zone_name: _Text = Field(max_length=100)


class RuleTemplate(Profile):
    """유형 하나의 규칙 확정 결과(`[rules.<유형>]`, SPEC-023 R-4)."""

    # 자리 표시는 `{zoneName}` 하나다. 구역 이름(100자)을 넣어도 description 상한(1000자) 안이다.
    template: _Text = Field(max_length=900)
    severity: Severity


class EscalationProfile(Profile):
    """유형 하나의 재판정 조건(`[escalation.<유형>]`, SPEC-023 R-6)."""

    # 탐지 점수가 이 구간 안(양 끝 포함)이면 1차 결과와 상관없이 재판정한다.
    ambiguous_score: tuple[float, float]

    @model_validator(mode="after")
    def _ordered_in_unit(self) -> Self:
        start, end = self.ambiguous_score
        if not (0 <= start <= end <= 1):
            raise ValueError("ambiguous_score must satisfy 0 <= start <= end <= 1")
        return self


class CapProfile(Profile):
    """실행당 VLM 호출 상한(`[cap]`, SPEC-023 R-19).

    상한 = max(floor, ceil(per_minute * 실행 길이(분))). 공유 카운터 없이 Job 합이 상한 이하다.
    기본값은 아키텍처 v5.1 17.2절(2분 청크 6회, 20초 창 3회)이다.
    """

    floor: int = Field(default=3, ge=1)
    per_minute: float = Field(default=3, gt=0)


class EventAnalyzerProfile(Profile):
    """event-analyzer 프로필 전체."""

    merge: MergeProfile = MergeProfile()
    rule_text: RuleTextProfile
    rules: dict[DetectableEventType, RuleTemplate]
    escalation: dict[DetectableEventType, EscalationProfile] = Field(default_factory=dict)
    cap: CapProfile = CapProfile()
    # 상태형 유형의 재판정 간격(초, SPEC-023 R-17). 원본은 이벤트 정의 목록의 rejudgeIntervalS이고
    # 계약과 같이 0보다 커야 한다. 없는 유형은 사건형이라 구간 규칙(carry)이 없다.
    rejudge: dict[DetectableEventType, Annotated[float, Field(gt=0)]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _every_type_has_a_rule(self) -> Self:
        # SPEC-023 D-8: 어떤 유형을 rule로 보낼지는 생산자의 이벤트 정의 목록이 정하고
        # event-analyzer는 그 목록을 모른다. 판정 중에 템플릿이 없는 일이 없도록 8종 모두 요구한다.
        missing = sorted(_DETECTABLE - set(self.rules))
        if missing:
            raise ValueError(f"rules are missing for event types: {', '.join(missing)}")
        return self

    def rejudge_interval_s(self, event_type: DetectableEventType) -> float | None:
        """상태형 유형의 재판정 간격. 사건형이면 None이다."""
        return self.rejudge.get(event_type)

    def is_ambiguous(self, event_type: DetectableEventType, score: float) -> bool:
        """탐지 점수가 그 유형의 애매 구간 안인지. 구간이 없는 유형은 False다."""
        escalation = self.escalation.get(event_type)
        if escalation is None:
            return False
        start, end = escalation.ambiguous_score
        return start <= score <= end
