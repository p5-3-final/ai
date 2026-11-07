"""OWL 계열(OWL-ViT·OWLv2) 개방 어휘 탐지기(plan 5절 C).

torch·transformers는 이 모듈에서만 import한다.

대상어 문장을 그대로 텍스트 쿼리로 넣어 프레임마다 박스를 찾는다. 가중치는 모델 버킷의 manifest로
받고, manifest modelId의 아키텍처로 OWL-ViT와 OWLv2를 고른다(docs ADR-0010 ②, SPEC-026 R-17).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import torch
from PIL import Image
from safetensors import SafetensorError
from transformers import (
    Owlv2ForObjectDetection,
    Owlv2Processor,
    OwlViTForObjectDetection,
    OwlViTProcessor,
)
from vlm_storage.artifacts import ArtifactError, fetch_model
from vlm_storage.storage import ObjectStorage

from grounding_worker.detector import (
    Box,
    Detection,
    DetectorError,
    DetectorLoadError,
    RgbFrame,
)
from grounding_worker.profile import OwlDetectorProfile

# 기동 확인용 빈 프레임 한 변(픽셀). 전처리가 모델 입력 크기로 늘리므로 크기는 결과에 영향이 없다.
_WARM_UP_SIDE = 32

type _Processor = OwlViTProcessor | Owlv2Processor
type _Model = OwlViTForObjectDetection | Owlv2ForObjectDetection


@dataclass(frozen=True, slots=True)
class _Family:
    processor: type[OwlViTProcessor] | type[Owlv2Processor]
    model: type[OwlViTForObjectDetection] | type[Owlv2ForObjectDetection]
    # OWLv2 전처리는 오른쪽·아래를 채워 정사각형을 만들고, OWL-ViT는 비율을 무시하고 늘린다.
    # 박스를 원본 좌표로 되돌리는 기준 크기가 달라진다.
    pads_to_square: bool


# manifest modelId(<라이브러리>:<아키텍처>:<가중치>)의 앞부분으로 구현을 고른다.
_FAMILIES = {
    "transformers:owlvit-": _Family(
        OwlViTProcessor, OwlViTForObjectDetection, pads_to_square=False
    ),
    "transformers:owlv2-": _Family(Owlv2Processor, Owlv2ForObjectDetection, pads_to_square=True),
}


class OwlDetector:
    """`load`로만 만든다. 모델은 eval 모드로 한 번 로드하고 프레임마다 다시 읽지 않는다."""

    def __init__(
        self,
        version: str,
        processor: _Processor,
        model: _Model,
        *,
        pads_to_square: bool,
        min_score: float,
        nms_iou: float,
    ) -> None:
        self._version = version
        self._processor = processor
        self._model = model
        self._pads_to_square = pads_to_square
        self._min_score = min_score
        self._nms_iou = nms_iou

    @classmethod
    def load(
        cls, models: ObjectStorage, profile: OwlDetectorProfile, cache_dir: Path
    ) -> "OwlDetector":
        """manifest와 가중치를 받아 확인한 뒤 모델을 로드한다. 블로킹이라 기동 단계에서 부른다.

        Raises:
            DetectorLoadError: 받지 못했거나 해시가 다르거나, manifest가 OWL 계열 개방 어휘
                탐지기가 아니거나, 가중치를 읽지 못했을 때.
        """
        folder, version, family = _fetch(models, profile, cache_dir)
        try:
            processor = family.processor.from_pretrained(folder, local_files_only=True)
            model = family.model.from_pretrained(folder, local_files_only=True)
        # 깨진 가중치는 SafetensorError, 설정 파일이 없거나 형식이 다르면 OSError·ValueError다.
        except (SafetensorError, OSError, ValueError, RuntimeError) as e:
            raise DetectorLoadError(f"failed to load {version}: {e}") from e
        # from_pretrained가 eval 모드로 돌려준다.
        detector = cls(
            version,
            processor,
            model,
            pads_to_square=family.pads_to_square,
            min_score=profile.score_threshold,
            nms_iou=profile.nms_iou,
        )
        detector._warm_up()
        return detector

    @property
    def version(self) -> str:
        return self._version

    def _target_size(self, frame: RgbFrame) -> tuple[int, int]:
        # 결과 박스를 이 (높이, 너비) 픽셀 좌표로 받는다. 정사각형으로 채운 입력은 긴 변 기준이다.
        if self._pads_to_square:
            side = max(frame.width, frame.height)
            return (side, side)
        return (frame.height, frame.width)

    def _warm_up(self) -> None:
        # 빈 프레임으로 한 번 돌려, 실행 시점에야 드러나는 문제(빠진 선택 의존성 등)를 메시지마다
        # FAILED로 내지 않고 기동 단계에서 멈춘다.
        try:
            self.detect(
                RgbFrame(
                    data=bytes(_WARM_UP_SIDE**2 * 3),
                    width=_WARM_UP_SIDE,
                    height=_WARM_UP_SIDE,
                    pts_ms=0,
                ),
                ["object"],
            )
        except DetectorError as e:
            raise DetectorLoadError(f"{self._version} warm-up failed: {e}") from e

    def detect(self, frame: RgbFrame, targets: Sequence[str]) -> list[Detection]:
        queries = list(targets)
        try:
            image = Image.frombytes("RGB", (frame.width, frame.height), frame.data)
            inputs = self._processor(text=[queries], images=image, return_tensors="pt")
            with torch.inference_mode():
                outputs = self._model(**inputs)
            result = self._processor.post_process_grounded_object_detection(
                outputs, threshold=self._min_score, target_sizes=[self._target_size(frame)]
            )[0]
        # ImportError: 전처리가 실행 시점에 선택 의존성(scipy 등)을 찾는다. 이미지에 빠졌을 때 난다.
        except (RuntimeError, ValueError, ImportError) as e:
            raise DetectorError(f"{self._version} failed at {frame.pts_ms}ms: {e}") from e
        boxes = [tuple(row) for row in result["boxes"].tolist()]
        scores = [float(score) for score in result["scores"].tolist()]
        labels = [int(label) for label in result["labels"].tolist()]
        return [
            Detection(
                queries[labels[i]],
                to_box(boxes[i], width=frame.width, height=frame.height),
                scores[i],
            )
            for i in suppress_overlaps(boxes, scores, labels, iou=self._nms_iou)
        ]


def to_box(xyxy: tuple[float, ...], *, width: int, height: int) -> Box:
    """픽셀 꼭짓점 좌표를 프레임 크기로 나눈 0~1 박스로 바꾸고 프레임 밖은 잘라 낸다."""
    x0, y0, x1, y1 = xyxy
    left, right = _clip(x0 / width), _clip(x1 / width)
    top, bottom = _clip(y0 / height), _clip(y1 / height)
    return Box(left, top, right - left, bottom - top)


def _clip(value: float) -> float:
    return min(max(value, 0.0), 1.0)


def _fetch(
    models: ObjectStorage, profile: OwlDetectorProfile, cache_dir: Path
) -> tuple[Path, str, _Family]:
    try:
        fetched = fetch_model(models, profile.manifest, profile.manifest_sha256, cache_dir)
    except ArtifactError as e:
        raise DetectorLoadError(str(e)) from e
    manifest = fetched.manifest
    if manifest.kind != "detector":
        raise DetectorLoadError(f"manifest kind {manifest.kind} is not detector")
    if not manifest.open_vocabulary:
        raise DetectorLoadError("manifest is not an open vocabulary detector")
    family = next((f for p, f in _FAMILIES.items() if manifest.model_id.startswith(p)), None)
    if family is None:
        raise DetectorLoadError(f"manifest model id {manifest.model_id} is not an OWL detector")
    # fetch_model은 manifest 폴더 하나에 파일을 모두 받는다. transformers는 그 폴더를 읽는다.
    [folder] = {path.parent for path in fetched.files.values()}
    return folder, manifest.version, family


def suppress_overlaps(
    boxes: Sequence[tuple[float, ...]],
    scores: Sequence[float],
    labels: Sequence[int],
    *,
    iou: float,
) -> list[int]:
    """대상어마다 점수가 높은 박스부터 남기고, 남긴 박스와 `iou` 이상 겹치는 박스를 버린다.

    기준 신뢰도로 거른 뒤라 박스가 수십 개 이하여서 단순 비교로 충분하다. 남긴 순서는 입력 순서다.
    """
    kept: list[int] = []
    for i in sorted(range(len(boxes)), key=lambda k: -scores[k]):
        if all(labels[k] != labels[i] or _iou(boxes[k], boxes[i]) < iou for k in kept):
            kept.append(i)
    return sorted(kept)


def _iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    width = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0
