"""event-analyzer 프로필 검증(SPEC-023 R-16). 틀린 프로필은 기동에 실패해야 한다."""

import json
import tomllib
from pathlib import Path
from typing import cast, get_args

import pytest
from ea_support import contracts_dir
from event_analyzer.profile import EventAnalyzerProfile
from vlm_common.profile import ProfileError, load_profile
from vlm_contracts.events import DetectableEventType

LOCAL = Path(__file__).resolve().parents[1] / "profiles" / "local.toml"
CONTRACTS = contracts_dir()
_TYPES: tuple[str, ...] = get_args(DetectableEventType.__value__)


def _raw() -> dict[str, object]:
    return tomllib.loads(LOCAL.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict[str, object]) -> Path:
    lines: list[str] = []

    def emit(table: str, values: dict[str, object]) -> None:
        scalars = {k: v for k, v in values.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in values.items() if isinstance(v, dict)}
        if table and scalars:
            lines.append(f"[{table}]")
        for key, value in scalars.items():
            lines.append(f"{key} = {_toml(value)}")
        for key, value in tables.items():
            emit(f"{table}.{key}" if table else key, value)

    emit("", raw)
    path = tmp_path / "profile.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _toml(value: object) -> str:
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    return str(value)


def test_spec023_r16_local_profile_loads_with_defaults() -> None:
    profile = load_profile(LOCAL, EventAnalyzerProfile)

    assert profile.merge.cooldown_s == 10
    assert set(profile.rules) == set(_TYPES)
    assert profile.rule_text.missing_zone_name


def test_spec023_r3_cooldown_defaults_to_ten_seconds(tmp_path: Path) -> None:
    raw = _raw()
    del raw["merge"]

    assert load_profile(_write(tmp_path, raw), EventAnalyzerProfile).merge.cooldown_s == 10


@pytest.mark.parametrize("cooldown", [0, -1])
def test_spec023_r16_non_positive_cooldown_fails(tmp_path: Path, cooldown: float) -> None:
    raw = _raw() | {"merge": {"cooldown_s": cooldown}}

    with pytest.raises(ProfileError, match="cooldown_s"):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)


@pytest.mark.parametrize("missing", ["hazard_zone_access", "fall"])
def test_spec023_r16_d8_every_detectable_type_needs_a_rule(tmp_path: Path, missing: str) -> None:
    """[SPEC-023 R-16, D-8] 어떤 유형을 rule로 보낼지는 생산자가 정하므로 8종 모두 요구한다."""
    raw = _raw()
    rules = dict(cast(dict[str, object], raw["rules"]))
    del rules[missing]
    raw["rules"] = rules

    with pytest.raises(ProfileError, match=missing):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)


@pytest.mark.parametrize(
    "rule",
    [
        {"template": "{zoneName} 진입"},
        {"severity": "medium"},
        {"template": "x" * 901, "severity": "medium"},
        {"template": "{zoneName} 진입", "severity": "critical"},
    ],
)
def test_spec023_r16_rule_needs_template_and_severity(
    tmp_path: Path, rule: dict[str, object]
) -> None:
    raw = _raw()
    raw["rules"] = {**cast(dict[str, object], raw["rules"]), "fall": rule}

    with pytest.raises(ProfileError):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)


@pytest.mark.parametrize("score", [[-0.1, 0.5], [0.4, 1.1], [0.7, 0.3], [0.5]])
def test_spec023_r16_ambiguous_score_range_must_be_ordered_in_unit(
    tmp_path: Path, score: list[float]
) -> None:
    raw = _raw() | {"escalation": {"ppe_missing": {"ambiguous_score": score}}}

    with pytest.raises(ProfileError):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)


def test_spec023_r6_ambiguous_range_is_optional_and_inclusive(tmp_path: Path) -> None:
    raw = _raw() | {"escalation": {"ppe_missing": {"ambiguous_score": [0.4, 0.4]}}}

    profile = load_profile(_write(tmp_path, raw), EventAnalyzerProfile)

    assert profile.is_ambiguous("ppe_missing", 0.4)
    assert not profile.is_ambiguous("ppe_missing", 0.41)
    assert not profile.is_ambiguous("fall", 0.4)


def test_spec023_r16_d9_missing_zone_name_is_required(tmp_path: Path) -> None:
    raw = _raw()
    del raw["rule_text"]

    with pytest.raises(ProfileError, match="rule_text"):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)


def test_spec023_r17_rejudge_intervals_mark_stateful_types() -> None:
    profile = load_profile(LOCAL, EventAnalyzerProfile)

    # 아키텍처 v5.1 8.2절의 기본 간격. fall처럼 사건형은 간격이 없다.
    assert profile.rejudge_interval_s("ppe_missing") == 120
    assert profile.rejudge_interval_s("fall") is None


@pytest.mark.parametrize("interval", [0, -5])
def test_spec023_r17_rejudge_interval_follows_taxonomy_lower_bound(
    tmp_path: Path, interval: float
) -> None:
    # 분류 체계 eventDefinition.rejudgeIntervalS는 exclusiveMinimum 0이다.
    taxonomy = json.loads(
        (CONTRACTS / "taxonomy/v2/taxonomy.schema.json").read_text(encoding="utf-8")
    )
    bound = taxonomy["$defs"]["eventDefinition"]["properties"]["rejudgeIntervalS"]
    assert bound["exclusiveMinimum"] == 0
    raw = _raw() | {"rejudge": {"ppe_missing": interval}}

    with pytest.raises(ProfileError, match="rejudge"):
        load_profile(_write(tmp_path, raw), EventAnalyzerProfile)
