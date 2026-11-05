"""API 테스트 공통 준비물: 가짜 Adapter·호출 로그와 앱 조립."""

import functools
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
import open_clip  # type: ignore[import-untyped]  # 타입 정보를 배포하지 않는다 (#19)
import torch
from ai_gateway.adapters.base import (
    Completion,
    Embeddings,
    ImageInput,
    TextEmbeddingAdapter,
    VlmAdapter,
)
from ai_gateway.analyze import CallLogWriter
from ai_gateway.call_log import CallLogEntry
from ai_gateway.main import GatewayDeps, create_app
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptMessage, PromptRegistry
from safetensors.torch import save
from vlm_encoders.base import ImageTextEncoder
from vlm_encoders.mock import MockEncoder
from vlm_storage.memory import InMemoryStorage
from vlm_storage.storage import ObjectStorage

SOURCE = UUID("0192f0d2-1a2b-7c3d-8e4f-5a6b7c8d9e0f")
CALLER = "event-analyzer"
IMAGE_KEYS = [
    f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/000.jpg",
    f"sources/{SOURCE}/chunks/0/candidates/c1/blurred/001.jpg",
]
CANDIDATE: dict[str, object] = {
    "eventType": "fall",
    "startSec": 61.2,
    "endSec": 66.2,
    "zoneName": "A동 적재 구역",
}


ENCODER_ID = "mock:MobileCLIP2-S0:dfndr2b:mean-4"
QUERY_IMAGE = "queries/0192f0d2-9a8b-7c6d-8e4f-5a6b7c8d9e0f/query.jpg"


def embed_settings() -> dict[str, object]:
    """임베딩 테스트 프로필의 `[encoder]`·`[embed.*]`. 고쳐서 `profile(extra=...)`로 넘긴다."""
    return {
        "encoder": {"kind": "mock", "model_id": ENCODER_ID, "dimension": 8},
        "embed": {
            "embed_visual_text": {"max_inputs": 3, "max_text_chars": 40},
            "embed_image": {
                "max_inputs": 2,
                "allowed_patterns": [r"^queries/[^/]+/[^/]+\.(jpg|png)$"],
            },
            "embed_text": {
                "adapter": "fake",
                "model": "fake-embedding",
                "dimension": 4,
                "max_inputs": 3,
                "max_text_chars": 40,
                "timeout_s": 5,
                "max_retries": 0,
            },
        },
    }


def parse_settings(**changes: object) -> dict[str, object]:
    """테스트 프로필의 `[parse]`. 기본은 가짜 Adapter의 `fake-llm`이다."""
    settings: dict[str, object] = {
        "max_query_chars": 50,
        "adapter": "fake",
        "model": "fake-llm",
        "max_output_tokens": 200,
        "timeout_s": 5,
        "max_retries": 0,
    }
    settings.update(changes)
    return settings


def profile(extra: dict[str, object] | None = None, **task: object) -> GatewayProfile:
    """테스트 프로필. `task`로 event_judge 설정을, `extra`로 최상위 항목을 바꾼다."""
    event_judge: dict[str, object] = {
        "adapter": "fake",
        "model": "fake-vlm",
        "max_output_tokens": 800,
        "max_images": 4,
        "timeout_s": 5,
        "max_retries": 0,
    }
    event_judge.update(task)
    return GatewayProfile.model_validate(
        {
            "callers": {"allowed": [CALLER]},
            "images": {"allowed_patterns": [r"^sources/{source_id}/.+/blurred/[^/]+\.(jpg|png)$"]},
            "adapters": {"fake": {"kind": "mock"}},
            "tasks": {"event_judge": event_judge},
            "parse": parse_settings(),
            **(extra or {}),
        }
    )


def output_example(registry_dir: Path, index: int = 0) -> dict[str, object]:
    schema = json.loads(
        (registry_dir / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
    )
    example: dict[str, object] = schema["examples"][index]
    return example


@dataclass(frozen=True)
class AdapterCall:
    model: str
    message: PromptMessage
    images: list[ImageInput]
    max_output_tokens: int
    traceparent: str | None


@dataclass
class FakeAdapter:
    """정해 둔 응답을 돌려주고 받은 요청을 기록한다."""

    reply: str | Exception = ""
    calls: list[AdapterCall] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        message: PromptMessage,
        images: list[ImageInput],
        max_output_tokens: int,
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
        temperature: float | None = None,
    ) -> Completion:
        self.calls.append(AdapterCall(model, message, images, max_output_tokens, traceparent))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Completion(text=self.reply, input_tokens=100, output_tokens=20)


@dataclass(frozen=True)
class EmbedCall:
    model: str
    texts: list[str]
    traceparent: str | None


@dataclass
class FakeEmbedder:
    """`vectors`를 그대로 돌려주고(없으면 차원 4의 결정적 벡터) 받은 요청을 기록한다."""

    reply: list[list[float]] | Exception | None = None
    calls: list[EmbedCall] = field(default_factory=list)

    async def embed(
        self,
        *,
        model: str,
        texts: list[str],
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Embeddings:
        self.calls.append(EmbedCall(model, texts, traceparent))
        if isinstance(self.reply, Exception):
            raise self.reply
        if self.reply is not None:
            return Embeddings(vectors=self.reply, input_tokens=7)
        return Embeddings(vectors=[[float(len(t)), 1.0, 0.0, 0.0] for t in texts], input_tokens=7)


@dataclass
class RecordingCallLog:
    entries: list[CallLogEntry] = field(default_factory=list)

    async def save(self, entry: CallLogEntry) -> None:
        self.entries.append(entry)


@dataclass
class Gateway:
    client: httpx.AsyncClient
    adapter: FakeAdapter
    call_log: RecordingCallLog
    storage: InMemoryStorage


@asynccontextmanager
async def gateway(
    registry_dir: Path,
    gateway_profile: GatewayProfile | None = None,
    adapter: FakeAdapter | None = None,
    prepare: Callable[[InMemoryStorage], None] | None = None,
    ready: Callable[[], bool] = lambda: True,
    adapters: dict[str, VlmAdapter] | None = None,
    call_log: CallLogWriter | None = None,
    encoder: ImageTextEncoder | None = None,
    embedders: dict[str, TextEmbeddingAdapter] | None = None,
) -> AsyncIterator[Gateway]:
    """가짜 Adapter·메모리 저장소·기록용 호출 로그로 앱을 띄운다."""
    storage = InMemoryStorage()
    for i, key in enumerate(IMAGE_KEYS):
        storage.put_bytes(key, f"jpeg-{i}".encode(), "image/jpeg")
    if prepare is not None:
        prepare(storage)
    fake = adapter or FakeAdapter()
    recording = RecordingCallLog()
    deps = GatewayDeps(
        profile=gateway_profile or profile(),
        prompts=PromptRegistry.load(registry_dir),
        storage=storage,
        adapters=adapters if adapters is not None else {"fake": fake},
        call_log=call_log if call_log is not None else recording,
        is_ready=ready,
        encoder=encoder,
        embedders=embedders or {},
    )

    async def build() -> GatewayDeps:
        return deps

    app = create_app(build)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-gateway") as client:
            yield Gateway(client, fake, recording, storage)


def analyze_body(**changes: object) -> dict[str, object]:
    body: dict[str, object] = {
        "taskType": "event_judge",
        "sourceId": str(SOURCE),
        "images": [{"storageKey": k, "ptsSec": 61.2 + i} for i, k in enumerate(IMAGE_KEYS)],
        "variables": CANDIDATE,
    }
    body.update(changes)
    return body


async def post_analyze(
    client: httpx.AsyncClient, body: dict[str, object], caller: str | None = CALLER
) -> httpx.Response:
    headers = {"X-Caller": caller} if caller is not None else {}
    return await client.post("/v1/analyze", json=body, headers=headers)


def mock_encoder() -> MockEncoder:
    return MockEncoder(ENCODER_ID, 8)


async def post_embed(
    client: httpx.AsyncClient, body: dict[str, object], caller: str | None = CALLER
) -> httpx.Response:
    headers = {"X-Caller": caller} if caller is not None else {}
    return await client.post("/v1/embed", json=body, headers=headers)


@functools.cache
def random_weights() -> bytes:
    """무작위로 초기화한 MobileCLIP2-S0 가중치 파일(safetensors). 실제 가중치를 받지 않는다."""
    torch.manual_seed(0)
    model = open_clip.create_model("MobileCLIP2-S0", pretrained=None)
    # 공유 텐서가 있으면 save가 거부하므로 state_dict를 복사해 넘긴다.
    tensors: dict[str, torch.Tensor] = {
        k: v.contiguous().clone() for k, v in model.state_dict().items()
    }
    return save(tensors)


ENCODER_MANIFEST_KEY = "models/mobileclip2-s0/dfndr2b/manifest.json"


def put_encoder_model(models: ObjectStorage, weights: bytes, dimension: int = 512) -> str:
    """가중치와 manifest를 모델 버킷에 올리고 프로필에 적을 manifest sha256을 돌려준다."""
    name = "open_clip_model.safetensors"
    manifest = {
        "kind": "image_text_encoder",
        "modelId": "open_clip:MobileCLIP2-S0:dfndr2b",
        "version": "dfndr2b",
        "files": [
            {"name": name, "sha256": hashlib.sha256(weights).hexdigest(), "size": len(weights)}
        ],
        "dimension": dimension,
        "source": {"type": "public", "origin": "huggingface:timm/MobileCLIP2-S0-OpenCLIP"},
        "license": "Apple AMLR",
    }
    raw = json.dumps(manifest).encode()
    prefix = ENCODER_MANIFEST_KEY.rsplit("/", 1)[0]
    models.put_bytes(f"{prefix}/{name}", weights, "application/octet-stream")
    models.put_bytes(ENCODER_MANIFEST_KEY, raw, "application/json")
    return hashlib.sha256(raw).hexdigest()
