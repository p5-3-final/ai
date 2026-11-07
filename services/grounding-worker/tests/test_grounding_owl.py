"""OWL 계열(OWL-ViT·OWLv2) 탐지기의 manifest 확인과 박스 후처리(SPEC-026 R-3, R-17).

실제 추론은 test_grounding_model.py(`model` 마커)에서 확인한다.
"""

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from grounding_worker.detector import Box, DetectorError, DetectorLoadError, RgbFrame, load_detector
from grounding_worker.detector_owl import OwlDetector, suppress_overlaps, to_box
from grounding_worker.profile import OwlDetectorProfile
from transformers import OwlViTForObjectDetection, OwlViTProcessor
from vlm_storage.memory import InMemoryStorage

MANIFEST_KEY = "models/owlvit/owlvit-b32/manifest.json"
WEIGHTS = b"not a real model"
WEIGHTS_SHA256 = hashlib.sha256(WEIGHTS).hexdigest()
DETECTOR: dict[str, object] = {
    "kind": "detector",
    "modelId": "transformers:owlvit-base-patch32:base",
    "version": "owlvit-b32",
    "files": [
        {
            "name": "model.safetensors",
            "sha256": hashlib.sha256(WEIGHTS).hexdigest(),
            "size": len(WEIGHTS),
        }
    ],
    "openVocabulary": True,
    "source": {"type": "public", "origin": "huggingface:google/owlvit-base-patch32"},
    "license": "Apache-2.0",
}


def _models(manifest: dict[str, object]) -> tuple[InMemoryStorage, OwlDetectorProfile]:
    storage = InMemoryStorage()
    raw = json.dumps(manifest).encode()
    storage.put_bytes(MANIFEST_KEY.replace("manifest.json", "model.safetensors"), WEIGHTS, "x")
    storage.put_bytes(MANIFEST_KEY, raw, "application/json")
    profile = OwlDetectorProfile(
        kind="owl",
        manifest=MANIFEST_KEY,
        manifest_sha256=hashlib.sha256(raw).hexdigest(),
        score_threshold=0.2,
    )
    return storage, profile


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"kind": "image_text_encoder", "dimension": 512, "openVocabulary": None}, "detector"),
        ({"openVocabulary": None, "classes": ["person"]}, "open vocabulary"),
        ({"modelId": "ultralytics:yoloe-11s:seg"}, "not an OWL"),
    ],
)
def test_spec026_r17_manifest_that_is_not_owl_fails_startup(
    tmp_path: Path, change: dict[str, object], reason: str
) -> None:
    manifest = {k: v for k, v in (DETECTOR | change).items() if v is not None}
    storage, profile = _models(manifest)

    with pytest.raises(DetectorLoadError, match=reason):
        load_detector(profile, allow_mock=False, models=storage, cache_dir=tmp_path)


def test_spec026_r17_manifest_hash_mismatch_fails_startup(tmp_path: Path) -> None:
    storage, profile = _models(DETECTOR)
    wrong = profile.model_copy(update={"manifest_sha256": "0" * 64})

    with pytest.raises(DetectorLoadError, match="sha256"):
        load_detector(wrong, allow_mock=False, models=storage, cache_dir=tmp_path)


@pytest.mark.parametrize(
    ("model_id", "version"),
    [
        ("transformers:owlvit-base-patch32:base", "owlvit-b32"),
        ("transformers:owlv2-base-patch16:ensemble", "owlv2-b16-ensemble"),
    ],
)
def test_spec026_r17_unreadable_weights_fail_startup(
    tmp_path: Path, model_id: str, version: str
) -> None:
    storage, profile = _models(DETECTOR | {"modelId": model_id, "version": version})

    with pytest.raises(DetectorLoadError, match=version):
        load_detector(profile, allow_mock=False, models=storage, cache_dir=tmp_path)


def test_spec026_r3_box_is_normalized_by_original_frame_size() -> None:
    # OWLv2 전처리는 오른쪽·아래를 채워 정사각형을 만든다. 픽셀 좌표를 원본 크기로 나눈다.
    box = to_box((480.0, 270.0, 960.0, 810.0), width=1920, height=1080)

    assert box == Box(x=0.25, y=0.25, w=0.25, h=0.5)


def test_spec026_r3_box_is_clipped_to_the_frame() -> None:
    box = to_box((-10.0, 1000.0, 2000.0, 1500.0), width=1920, height=1080)

    assert box.x == 0
    assert box.x + box.w == pytest.approx(1)
    assert box.y == pytest.approx(1000 / 1080)
    assert box.y + box.h == pytest.approx(1)


def test_suppress_overlaps_keeps_best_box_per_target() -> None:
    boxes = [
        (0.0, 0.0, 10.0, 10.0),
        (1.0, 1.0, 10.0, 10.0),
        (0.0, 0.0, 10.0, 10.0),
        (50.0, 50.0, 60.0, 60.0),
    ]
    scores = [0.5, 0.9, 0.7, 0.3]
    labels = [0, 0, 1, 0]

    kept = suppress_overlaps(boxes, scores, labels, iou=0.3)

    # 같은 대상어(0)에서 많이 겹치는 0번은 버리고, 다른 대상어(1)와 떨어진 박스는 남긴다.
    assert kept == [1, 2, 3]


class _BrokenProcessor:
    """선택 의존성(scipy 등)이 이미지에 없을 때 transformers 전처리가 내는 오류를 흉내 낸다."""

    def __call__(self, **_: object) -> object:
        raise ImportError("Owlv2ImageProcessorPil requires the scipy library")


def test_spec026_r12_library_import_error_becomes_detector_error() -> None:
    # 전처리 단계에서 실패하므로 모델은 쓰이지 않는다.
    processor = cast(OwlViTProcessor, _BrokenProcessor())
    model = cast(OwlViTForObjectDetection, None)
    detector = OwlDetector("v1", processor, model, pads_to_square=False, min_score=0.1, nms_iou=0.3)
    frame = RgbFrame(data=bytes(4 * 4 * 3), width=4, height=4, pts_ms=1_000)

    with pytest.raises(DetectorError, match="scipy"):
        detector.detect(frame, ["person"])
