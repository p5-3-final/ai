"""실제 OWL-ViT 가중치로 확인한다(마커 `model`, CI 제외).

실행: `uv run pytest services/grounding-worker -m model`.

가중치는 Hugging Face 고정 리비전에서 받아(이후 그 캐시를 쓴다) docs
`local/scripts/upload-models.sh`와 같은 manifest를 만들어, 로컬 프로필의 manifest sha256부터
파일 sha256까지 맞는지 본다.
"""

import hashlib
import io
import json
import logging
import time
from pathlib import Path

import httpx
import pytest
from grounding_worker.detector import GroundingDetector, RgbFrame, load_detector
from grounding_worker.profile import GroundingProfile, OwlDetectorProfile
from huggingface_hub import hf_hub_download
from PIL import Image
from vlm_common.profile import load_profile
from vlm_storage.memory import InMemoryStorage

pytestmark = pytest.mark.model

logger = logging.getLogger(__name__)

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "local.toml"
REPO = "google/owlvit-base-patch32"
REVISION = "cbc355fb364588351c5d51c7f74465e8e7ec6f72"
# upload-models.sh의 파일 목록과 순서가 같아야 manifest 바이트(프로필의 sha256)가 같다.
FILES = [
    "config.json",
    "merges.txt",
    "model.safetensors",
    "preprocessor_config.json",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.json",
]
# frame-encoder·ai-gateway 모델 테스트와 같은 공개 라이선스 사진(Wikimedia Commons).
FORKLIFT = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg/960px-"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg"
)


@pytest.fixture(scope="module")
def detector(tmp_path_factory: pytest.TempPathFactory) -> GroundingDetector:
    profile = load_profile(PROFILE, GroundingProfile)
    models = InMemoryStorage()
    assert isinstance(profile.detector, OwlDetectorProfile)
    prefix = profile.detector.manifest.removesuffix("manifest.json")
    entries = []
    for name in FILES:
        data = Path(hf_hub_download(REPO, name, revision=REVISION)).read_bytes()
        models.put_bytes(prefix + name, data, "application/octet-stream")
        entries.append(
            {"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        )
    manifest = {
        "kind": "detector",
        "modelId": "transformers:owlvit-base-patch32:base",
        "version": "owlvit-b32",
        "files": entries,
        "openVocabulary": True,
        "source": {"type": "public", "origin": f"huggingface:{REPO}@{REVISION}"},
        "license": "Apache-2.0",
    }
    raw = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode()
    models.put_bytes(prefix + "manifest.json", raw, "application/json")
    # SPEC-026 R-17: 프로필의 manifest sha256이 공개 가중치로 만든 manifest와 다르면 여기서
    # 실패한다.
    return load_detector(
        profile.detector,
        allow_mock=False,
        models=models,
        cache_dir=tmp_path_factory.mktemp("cache"),
    )


@pytest.fixture(scope="module")
def forklift(pytestconfig: pytest.Config) -> RgbFrame:
    """사진을 한 번만 받아 pytest 캐시에 둔다. Wikimedia는 같은 주소를 자주 받으면 429로 막는다."""
    cache = pytestconfig.cache
    assert cache is not None
    # 다른 모델 테스트와 같은 캐시 폴더를 써서 다시 받지 않는다.
    path = cache.mkdir("spec011-photos") / "forklift.jpg"
    if not path.exists():
        # Wikimedia User-Agent 정책: 도구 이름과 연락처를 적는다.
        agent = "vlm-ai-tests/0.1 (https://github.com/p5-3-final/ai)"
        response = httpx.get(FORKLIFT, headers={"User-Agent": agent}, timeout=30)
        response.raise_for_status()
        path.write_bytes(response.content)
    with Image.open(io.BytesIO(path.read_bytes())) as image:
        rgb = image.convert("RGB")
        return RgbFrame(data=rgb.tobytes(), width=rgb.width, height=rgb.height, pts_ms=0)


def test_spec026_r17_version_comes_from_manifest(detector: GroundingDetector) -> None:
    assert detector.version == "owlvit-b32"


def test_spec026_r3_finds_target_inside_the_frame(
    detector: GroundingDetector, forklift: RgbFrame
) -> None:
    """[SPEC-026 R-3] 대상어를 텍스트 쿼리로 넣으면 프레임 안 박스를 기준 신뢰도 이상으로 낸다."""
    started = time.perf_counter()
    detections = detector.detect(forklift, ["forklift"])
    elapsed = time.perf_counter() - started

    logger.info(
        "owl frame detected", extra={"elapsedS": round(elapsed, 2), "count": len(detections)}
    )
    assert detections
    best = max(detections, key=lambda d: d.confidence)
    assert best.target == "forklift"
    assert best.confidence >= 0.1
    box = best.box
    assert box.x >= 0
    assert box.x + box.w <= 1 + 1e-6
    assert box.y >= 0
    assert box.y + box.h <= 1 + 1e-6
    # 지게차가 사진의 큰 부분을 차지한다. 작은 오검출 박스가 최고 점수가 아닌지 본다.
    assert box.w * box.h > 0.05
