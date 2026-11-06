"""track-stitcher 기준값 프로필(TOML). 연결 기준은 코드가 아니라 이 값으로 바꾼다.

프로필이 없거나 값이 범위 밖이면 기동에 실패한다(SPEC-022 R-22). 잘못된 기준으로 트랙을 잇거나
끊으면 체류·배회 규칙이 조용히 틀어지므로 멈추는 편이 안전하다.
"""

from pydantic import Field
from vlm_common.profile import Profile


class BoundaryProfile(Profile):
    """경계 연결 기준(`[boundary]`, R-4·R-6)."""

    min_mean_iou: float = Field(ge=0, le=1)
    min_common_observations: int = Field(ge=1)
    time_tolerance_ms: int = Field(ge=0)


class ReidProfile(Profile):
    """재등장 연결 기준(`[reid]`, R-9)."""

    min_similarity: float = Field(ge=0, le=1)
    max_gap_ms: int = Field(ge=1)


class FinalizeProfile(Profile):
    """확정 기준(`[finalize]`, R-15)."""

    wait_timeout_s: int = Field(default=1800, ge=1)


class StitchProfile(Profile):
    """track-stitcher 프로필 전체."""

    # track.manifest·번역표의 stitchProfile. 기준값을 바꾸면 올린다.
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}$")
    boundary: BoundaryProfile
    reid: ReidProfile
    finalize: FinalizeProfile = FinalizeProfile()
