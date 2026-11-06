"""실제 MobileCLIP2-S0 가중치로 확인한다(마커 `model`, CI 제외).

실행: `uv run pytest services/frame-encoder -m model`.

가중치는 Hugging Face에서 받아(이후 그 캐시를 쓴다) docs `local/scripts/upload-models.sh`와 같은
manifest를 만들어, 로컬 프로필의 manifest sha256부터 가중치 sha256까지 맞는지 본다.
"""

import hashlib
import json
import logging
import math
import time
from pathlib import Path

import httpx
import pytest
from fe_support import PREFIX, RecordingPublisher, segment_ready
from frame_encoder.__main__ import Prepared, prepare
from frame_encoder.ids import vectors_key
from frame_encoder.service import FrameEncoderService
from frame_encoder.settings import FrameEncoderSettings
from frame_encoder.vectors import cosine
from huggingface_hub import hf_hub_download
from vlm_contracts.events import SegmentEmbedded
from vlm_contracts.vectors import VectorFile
from vlm_messaging.worker import MessageContext
from vlm_storage.memory import InMemoryStorage

pytestmark = pytest.mark.model

logger = logging.getLogger(__name__)

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "local.toml"
MANIFEST = "models/mobileclip2-s0/dfndr2b/manifest.json"
# ai-gateway의 같은 테스트와 같은 공개 라이선스 사진(Wikimedia Commons).
FORKLIFT = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg/960px-"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg"
)
FIRE = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b9/"
    "Structure_Fire_in_Union%2C_Mississippi_01.jpg/960px-Structure_Fire_in_Union%2C_Mississippi_01.jpg"
)
CTX = MessageContext(
    topic="segment.ready", partition=0, offset=0, key=None, attempt=1, traceparent=None
)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory: pytest.TempPathFactory) -> Prepared:
    name = "open_clip_model.safetensors"
    data = Path(hf_hub_download("timm/MobileCLIP2-S0-OpenCLIP", name)).read_bytes()
    # upload-models.sh의 MANIFEST와 직렬화 형식이 같아야 프로필의 manifest sha256과 맞는다.
    manifest = {
        "kind": "image_text_encoder",
        "modelId": "open_clip:MobileCLIP2-S0:dfndr2b",
        "version": "dfndr2b",
        "files": [{"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}],
        "dimension": 512,
        "source": {"type": "public", "origin": "huggingface:timm/MobileCLIP2-S0-OpenCLIP"},
        "license": "Apple AMLR (non-commercial research)",
    }
    models = InMemoryStorage()
    models.put_bytes(MANIFEST.replace("manifest.json", name), data, "application/octet-stream")
    models.put_bytes(
        MANIFEST,
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(),
        "application/json",
    )
    settings = FrameEncoderSettings(
        profile_path=PROFILE, model_cache_dir=tmp_path_factory.mktemp("cache")
    )
    # SPEC-019 R-9: 프로필의 manifest sha256이 공개 가중치로 만든 manifest와 다르면 여기서 실패한다.
    return prepare(settings, models)


@pytest.fixture(scope="module")
def photos(pytestconfig: pytest.Config) -> dict[str, bytes]:
    """사진을 한 번만 받아 pytest 캐시에 둔다. Wikimedia는 같은 주소를 자주 받으면 429로 막는다."""
    cache = pytestconfig.cache
    assert cache is not None
    # ai-gateway 테스트와 같은 캐시 폴더를 써서 다시 받지 않는다.
    folder = cache.mkdir("spec011-photos")
    found: dict[str, bytes] = {}
    for name, url in {"forklift": FORKLIFT, "fire": FIRE}.items():
        path = folder / f"{name}.jpg"
        if not path.exists():
            # Wikimedia User-Agent 정책: 도구 이름과 연락처를 적는다.
            agent = "vlm-ai-tests/0.1 (https://github.com/p5-3-final/ai)"
            response = httpx.get(url, headers={"User-Agent": agent}, timeout=30)
            response.raise_for_status()
            path.write_bytes(response.content)
        found[name] = path.read_bytes()
    return found


async def test_spec_019_nfr2_full_message_is_processed_within_120s(
    prepared: Prepared, photos: dict[str, bytes]
) -> None:
    """[SPEC-019 NFR-2, R-8] 구간 24개(프레임 96장)·크롭 30장을 120초 안에 처리한다."""
    ready = segment_ready(segments=24, crops=30)
    storage = InMemoryStorage()
    keys = [f.key for s in ready.segments for f in s.frames] + [c.key for c in ready.crops]
    for i, key in enumerate(keys):
        storage.put_bytes(key, photos["fire" if i % 2 else "forklift"], "image/jpeg")
    publisher = RecordingPublisher()
    service = FrameEncoderService(storage, prepared.encoder, publisher, prepared.config)

    started = time.perf_counter()
    await service.handle(ready, CTX)
    elapsed = time.perf_counter() - started

    logger.info("full message encoded", extra={"elapsedS": round(elapsed, 2)})
    assert elapsed < 120
    stored = VectorFile.model_validate_json(storage.get_bytes(vectors_key(ready)))
    assert (stored.embedding_model, stored.dimension) == (
        "open_clip:MobileCLIP2-S0:dfndr2b:mean-4",
        512,
    )
    vectors = [s.vector for s in stored.segments] + [c.vector for c in stored.crops]
    assert len(vectors) == 54
    assert all(math.isclose(math.hypot(*v), 1.0, abs_tol=1e-5) for v in vectors)
    [embedded, *_] = publisher.published
    assert isinstance(embedded[1], SegmentEmbedded)


async def test_spec_019_fire_segment_is_closer_to_fire_prototype(
    prepared: Prepared, photos: dict[str, bytes]
) -> None:
    """[SPEC-019 R-11] 화재 사진 구간이 지게차 사진 구간보다 화재 프로토타입 점수가 높다."""
    prototypes = prepared.config.prototypes
    assert prototypes is not None
    ready = segment_ready(segments=2, crops=0)
    storage = InMemoryStorage()
    for segment, photo in zip(ready.segments, ["fire", "forklift"], strict=True):
        for frame in segment.frames:
            storage.put_bytes(frame.key, photos[photo], "image/jpeg")
    service = FrameEncoderService(storage, prepared.encoder, RecordingPublisher(), prepared.config)

    await service.handle(ready, CTX)

    stored = VectorFile.model_validate_json(storage.get_bytes(f"{PREFIX}vectors.json"))
    fire, forklift = (cosine(s.vector, prototypes.vector("fire_or_smoke")) for s in stored.segments)
    logger.info("fire prototype scores", extra={"fire": fire, "forklift": forklift})
    assert fire > forklift
