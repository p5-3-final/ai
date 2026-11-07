"""탐지기 인터페이스와 결정적 mock 구현(SPEC-026 R-3).

서비스 코드는 `GroundingDetector`만 쓴다. 실제 구현(라이브러리)은 사용자 확인 뒤 더한다(plan 5절).
"""

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from vlm_common.errors import VlmError
from vlm_common.profile import ProfileError

from grounding_worker.profile import MockDetectorProfile


@dataclass(frozen=True, slots=True)
class RgbFrame:
    """ffmpeg가 내보낸 rgb24 프레임. 이미지 라이브러리 타입에 묶이지 않는다.

    Attributes:
        data: 행 우선 rgb24 바이트(너비 x 높이 x 3).
        pts_ms: 정규화 영상 기준 표시 시각.
    """

    data: bytes
    width: int
    height: int
    pts_ms: int


@dataclass(frozen=True, slots=True)
class Box:
    """프레임 크기로 나눈 0~1 좌표. 왼쪽 위 기준."""

    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True, slots=True)
class Detection:
    """탐지 하나. 기준 신뢰도 필터는 서비스가 한다(구현마다 기준이 어긋나지 않게)."""

    target: str
    box: Box
    confidence: float


class DetectorError(VlmError):
    """탐지기가 결과를 내지 못했다(SPEC-026 R-12). 구현은 자기 라이브러리 오류를 이것으로 바꾼다."""

    code = "GROUNDING_DETECTOR_FAILED"


class GroundingDetector(Protocol):
    """대상어를 텍스트 클래스로 쓰는 개방 어휘 탐지기. 블로킹이고 CPU를 쓴다."""

    @property
    def version(self) -> str:
        """groundingModel(가중치 manifest의 version)."""
        ...

    def detect(self, frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        """프레임에서 대상어를 찾는다. 실패하면 `DetectorError`."""
        ...


class MockDetector:
    """대상어와 프레임 시각으로 박스를 결정적으로 만든다. CI·로컬 흐름 확인 전용이다."""

    def __init__(self, version: str) -> None:
        self._version = version

    @property
    def version(self) -> str:
        return self._version

    def detect(self, frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        return [self._detect(frame.pts_ms, target) for target in targets]

    @staticmethod
    def _detect(pts_ms: int, target: str) -> Detection:
        digest = hashlib.sha256(f"{target}|{pts_ms}".encode()).digest()
        w, h = 0.2, 0.3
        # 바이트 값(0~255)을 남은 폭 안의 위치로 옮겨 박스가 프레임 밖으로 나가지 않게 한다.
        x = digest[0] / 255 * (1 - w)
        y = digest[1] / 255 * (1 - h)
        confidence = 0.5 + digest[2] / 255 * 0.5
        return Detection(target, Box(x, y, w, h), confidence)


def load_detector(profile: MockDetectorProfile, *, allow_mock: bool) -> GroundingDetector:
    """프로필의 탐지기를 만든다. 기동 때 한 번 부른다.

    Raises:
        ProfileError: mock을 허용하지 않았는데 mock을 고른 경우. mock은 CI·로컬 전용이다.
    """
    if not allow_mock:
        raise ProfileError("mock detector requires GROUNDING_WORKER_ALLOW_MOCK=true")
    return MockDetector(profile.version)
