"""aggregator 프로필(TOML). 병합 간격·시간 규칙 기준값·템플릿은 코드 상수가 아니라 프로필 값이다."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field
from vlm_common.profile import Profile, load_profile

DEFAULT_PROFILE_PATH = Path(__file__).resolve().parents[2] / "profiles" / "aggregate-v1.toml"
"""레포에 둔 기본 프로필. 이미지에는 `/app/profiles/aggregate-v1.toml`로 들어간다."""


class AggregateProfile(Profile):
    """결과를 정하는 기준값(R-6, R-8~R-10, R-31, SPEC-028). 기본값은 spec 4절의 기본값이다."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
    merge_gap_ms: int = Field(default=10_000, ge=0)
    dwell_ms: int = Field(default=60_000, ge=1)
    loiter_window_ms: int = Field(default=300_000, ge=1)
    loiter_count: int = Field(default=3, ge=2)
    person_classes: tuple[str, ...] = ("person",)
    dwell_description: str = Field(min_length=1, max_length=300)
    loiter_description: str = Field(min_length=1, max_length=300)
    judge_call_limit: int = Field(default=20, ge=0)
    snapshot_every_windows: int = Field(default=30, ge=1)
    # SPEC-028: 재고 비교·장비 방치(R-3, R-5, R-6, R-11, R-19)
    inventory_iou: float = Field(default=0.5, gt=0, le=1)
    move_window_ms: int = Field(default=120_000, ge=0)
    barrier_classes: tuple[str, ...] = ()
    equipment_classes: tuple[str, ...] = ()
    abandon_ms: int = Field(default=600_000, ge=1)
    abandon_description: str = Field(
        default="장비가 '{zone}'에 사람 없이 {minutes}분 이상 멈춰 있었습니다.",
        min_length=1,
        max_length=300,
    )
    stream_inventory_interval_s: int = Field(default=600, ge=1)


@dataclass(frozen=True, slots=True)
class LoadedProfile:
    """검증한 프로필과 그 버전(`aggregateProfile`)."""

    profile: AggregateProfile
    version: str


def load_aggregate_profile(path: Path) -> LoadedProfile:
    """프로필을 읽고 버전 `<name>@<파일 내용 sha256 앞 12자리>`를 붙인다(R-21).

    같은 이름이라도 기준값이 바뀌면 버전이 바뀌어, 결과 파일만 보고 어떤 기준으로 계산했는지 안다.

    Raises:
        ProfileError: 파일이 없거나 스키마와 맞지 않을 때.
    """
    profile = load_profile(path, AggregateProfile)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    return LoadedProfile(profile, f"{profile.name}@{digest}")
