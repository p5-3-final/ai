"""grounding-worker 프로필(TOML). 탐지기·기준 신뢰도·렌더링 설정은 코드가 아니라 이 값으로 바꾼다.

프로필이 틀리면 기동에 실패한다. 잘못된 기준으로 클립을 만들기보다 멈추는 편이 안전하다.
"""

import re
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from vlm_common.profile import Profile
from vlm_contracts.events import GROUNDING_MODEL_PATTERN

# ffmpeg 필터 스크립트에 그대로 넣는 경로의 허용 문자. 필터 문법의 특수 문자(`'`, `:`, `,`, `;`,
# `[`, `]`, `\`)가 없으면 이스케이프가 필요 없어 필터 주입을 원천적으로 막는다.
SAFE_PATH_PATTERN = r"^/[A-Za-z0-9_./-]+$"

type X264Preset = Literal[
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"
]


class MockDetectorProfile(Profile):
    """결정적 가짜 탐지기(`[detector] kind = "mock"`). CI·로컬 흐름 확인 전용이다.

    실제 탐지기 종류는 라이브러리를 정한 뒤 판별 공용체로 더한다(tasks T026~T027).
    """

    kind: Literal["mock"]
    # groundingModel(가중치 manifest의 version 자리). 결과 경로 한 단계가 된다.
    version: str = Field(pattern=GROUNDING_MODEL_PATTERN)
    # SPEC-026 R-3: 이 값 이상인 탐지만 남긴다. 값은 AI 담당자가 정한다.
    score_threshold: float = Field(ge=0, le=1)


class RenderProfile(Profile):
    """주석 클립 인코딩과 그리기 설정(`[render]`)."""

    preset: X264Preset = "veryfast"
    crf: int = Field(default=23, ge=0, le=51)
    line_width: int = Field(default=3, ge=1, le=20)
    font_path: Path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    font_size: int = Field(default=24, ge=8, le=128)

    @field_validator("font_path")
    @classmethod
    def _safe_font_path(cls, value: Path) -> Path:
        if not re.fullmatch(SAFE_PATH_PATTERN, str(value)):
            raise ValueError("font_path must be an absolute path of [A-Za-z0-9_./-]")
        return value


class GroundingProfile(Profile):
    """프로필 전체."""

    detector: MockDetectorProfile
    render: RenderProfile = RenderProfile()
