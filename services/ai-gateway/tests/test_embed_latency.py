"""실제 MobileCLIP2-S0 가중치로 확인한다(마커 `model`, CI 제외).

실행: `uv run pytest -m model`.

가중치는 Hugging Face에서 받아(이후 그 캐시를 쓴다) docs `local/scripts/upload-models.sh`와 같은
manifest를 만들어, 로컬 프로필의 manifest sha256부터 가중치 sha256까지 맞는지 본다.
"""

import hashlib
import json
import logging
import statistics
import time
from pathlib import Path

import httpx
import pytest
from ai_gateway.main import load_encoder
from ai_gateway.profile import GatewayProfile
from gateway_support import QUERY_IMAGE, embed_settings, gateway, post_embed, profile
from huggingface_hub import hf_hub_download
from vlm_common.profile import load_profile
from vlm_encoders.base import ImageTextEncoder
from vlm_storage.memory import InMemoryStorage

pytestmark = pytest.mark.model

logger = logging.getLogger(__name__)

PROFILE = Path(__file__).resolve().parents[1] / "profiles" / "local.toml"
# 공개 라이선스 사진(Wikimedia Commons). 지게차가 있는 창고와 지게차가 없는 화재 현장.
FORKLIFT = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/c/ca/"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg/960px-"
    "Forklift_parked_inside_a_warehouse_in_an_industrial_setting.jpg"
)
FIRE = (
    "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b9/"
    "Structure_Fire_in_Union%2C_Mississippi_01.jpg/960px-Structure_Fire_in_Union%2C_Mississippi_01.jpg"
)


@pytest.fixture(scope="module")
def encoder(tmp_path_factory: pytest.TempPathFactory) -> ImageTextEncoder:
    local = load_profile(PROFILE, GatewayProfile)
    assert local.encoder is not None
    assert local.encoder.manifest is not None
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
    models.put_bytes(
        local.encoder.manifest.replace("manifest.json", name), data, "application/octet-stream"
    )
    models.put_bytes(
        local.encoder.manifest,
        (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(),
        "application/json",
    )
    # 프로필의 manifest sha256이 공개 가중치로 만든 manifest와 다르면 여기서 EncoderLoadError다.
    loaded = load_encoder(local, models, tmp_path_factory.mktemp("cache"))
    assert loaded is not None
    return loaded


@pytest.fixture(scope="module")
def photos(pytestconfig: pytest.Config) -> dict[str, bytes]:
    """사진을 한 번만 받아 pytest 캐시에 둔다. Wikimedia는 같은 주소를 자주 받으면 429로 막는다."""
    cache = pytestconfig.cache
    assert cache is not None
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


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_spec011_forklift_text_is_closer_to_forklift_image(
    encoder: ImageTextEncoder, photos: dict[str, bytes]
) -> None:
    """[SPEC-011 성공 지표] "a forklift" 벡터가 지게차 없는 사진보다 지게차 사진과 더 가깝다."""
    [text] = encoder.encode_texts(["a forklift"])
    forklift, fire = encoder.encode_images([photos["forklift"], photos["fire"]])

    assert _dot(text, forklift) > _dot(text, fire)


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20)[-1]


async def test_spec011_nfr1_nfr2_latency(
    registry_dir: Path, encoder: ImageTextEncoder, photos: dict[str, bytes]
) -> None:
    """[SPEC-011 NFR-1, NFR-2] 문장 1개 p95 150ms, 이미지 1장 p95 300ms 이하(CPU)."""
    image = photos["forklift"]

    async with gateway(
        registry_dir,
        profile(embed_settings() | {"encoder": _local_encoder_settings()}),
        encoder=encoder,
        prepare=lambda s: s.put_bytes(QUERY_IMAGE, image, "image/jpeg"),
    ) as gw:
        texts: list[float] = []
        images: list[float] = []
        for i in range(40):
            started = time.perf_counter()
            await post_embed(gw.client, {"taskType": "embed_visual_text", "texts": [f"scene {i}"]})
            texts.append(time.perf_counter() - started)
            started = time.perf_counter()
            await post_embed(
                gw.client, {"taskType": "embed_image", "images": [{"storageKey": QUERY_IMAGE}]}
            )
            images.append(time.perf_counter() - started)

    # PR에 남길 측정값. `-o log_cli=true --log-cli-level=INFO`로 본다.
    logger.info("embed_visual_text p95=%.0fms", _p95(texts) * 1000)
    logger.info("embed_image p95=%.0fms", _p95(images) * 1000)
    assert _p95(texts) <= 0.150
    assert _p95(images) <= 0.300


def _local_encoder_settings() -> dict[str, object]:
    local = load_profile(PROFILE, GatewayProfile)
    assert local.encoder is not None
    return local.encoder.model_dump()
