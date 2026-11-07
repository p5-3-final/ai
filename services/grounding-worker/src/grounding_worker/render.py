"""주석 그리기: 대상어 색, 샘플별 표시 구간, ffmpeg 필터 스크립트(SPEC-026 R-4, R-5).

박스는 ffmpeg `drawbox`·`drawtext`로 그린다. 프레임 데이터가 Python을 거치지 않아 메모리와 CPU가
적게 든다(plan 5절).
"""

import hashlib
import itertools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from grounding_worker.detector import Detection
from grounding_worker.profile import SAFE_PATH_PATTERN, RenderProfile

# 라벨 글자 둘레 여백(px)
_LABEL_PAD = 4


@dataclass(frozen=True, slots=True)
class Window:
    """샘플 하나의 박스를 보이는 클립 기준 시각 범위 `[start_s, end_s)`. None은 끝이 열림."""

    start_s: float | None
    end_s: float | None

    def enable(self) -> str:
        """ffmpeg `enable` 식.

        `between`은 양 끝을 포함해 경계 프레임에 두 샘플이 겹치므로 쓰지 않는다.
        """
        parts = []
        if self.start_s is not None:
            parts.append(f"gte(t,{self.start_s:g})")
        if self.end_s is not None:
            parts.append(f"lt(t,{self.end_s:g})")
        return "*".join(parts) or "1"


def target_color(target: str) -> str:
    """SPEC-026 R-5: 대상어 UTF-8 바이트 sha256의 앞 3바이트.

    요청이 달라도 같은 대상어는 같은 색이다.
    """
    return "#" + hashlib.sha256(target.encode("utf-8")).hexdigest()[:6]


def box_windows(sample_pts_ms: Sequence[int], start_ms: int) -> list[Window]:
    """샘플마다 표시 구간. 경계는 이웃 샘플과의 중간점이다.

    SPEC-026 R-4: 모든 원본 프레임이 표시 시각이 가장 가까운 샘플의 박스를 받는다. 클립은 구간
    시작에서 0초로 시작하므로 클립 기준 초로 바꾼다.
    """
    mids = [((a + b) / 2 - start_ms) / 1000 for a, b in itertools.pairwise(sample_pts_ms)]
    starts: list[float | None] = [None, *mids]
    ends: list[float | None] = [*mids, None]
    return [Window(s, e) for s, e in zip(starts, ends, strict=True)]


def label_files(targets: Sequence[str], folder: Path) -> dict[str, Path]:
    """대상어 라벨을 파일로 쓴다. `drawtext`에 `textfile`로 넘긴다.

    대상어를 필터 문자열에 넣지 않으므로 `'` 같은 글자가 있어도 이스케이프할 일이 없다.

    Raises:
        ValueError: `folder`에 필터 문법의 특수 문자가 있을 때.
    """
    _check_safe(folder)
    paths: dict[str, Path] = {}
    for index, target in enumerate(targets):
        path = folder / f"label-{index}.txt"
        path.write_text(target, encoding="utf-8")
        paths[target] = path
    return paths


def filter_script(
    detections: Sequence[Sequence[Detection]],
    windows: Sequence[Window],
    *,
    width: int,
    height: int,
    labels: Mapping[str, Path],
    profile: RenderProfile,
) -> str:
    """샘플별 탐지를 그리는 필터 스크립트(`-/filter:v`로 넘긴다). 그릴 것이 없으면 `null` 필터다."""
    filters: list[str] = []
    for found, window in zip(detections, windows, strict=True):
        for detection in found:
            filters += _draw(detection, window, width, height, labels, profile)
    return ",\n".join(filters) or "null"


def _draw(
    detection: Detection,
    window: Window,
    width: int,
    height: int,
    labels: Mapping[str, Path],
    profile: RenderProfile,
) -> list[str]:
    box = detection.box
    x = min(round(box.x * width), width - 1)
    y = min(round(box.y * height), height - 1)
    w = max(1, min(round(box.w * width), width - x))
    h = max(1, min(round(box.h * height), height - y))
    color = "0x" + target_color(detection.target)[1:]
    enable = f"enable='{window.enable()}'"
    label_y = max(y - profile.font_size - 2 * _LABEL_PAD, 0)
    label = _check_safe(labels[detection.target])
    font = _check_safe(profile.font_path)
    return [
        f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t={profile.line_width}:{enable}",
        f"drawtext=fontfile={font}:textfile={label}:expansion=none:x={x}:y={label_y}"
        f":fontsize={profile.font_size}:fontcolor=white:box=1:boxcolor={color}"
        f":boxborderw={_LABEL_PAD}:{enable}",
    ]


def _check_safe(path: Path) -> Path:
    # 필터 스크립트에 넣는 경로는 이스케이프 대신 허용 문자로 제한한다(plan 3절 VII).
    if not re.fullmatch(SAFE_PATH_PATTERN, str(path)):
        raise ValueError(f"path has characters not allowed in a filter script: {path}")
    return path
