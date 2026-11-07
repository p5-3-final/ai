"""API 테스트 공통 준비물: 가짜 Adapter·호출 로그와 앱 조립."""

import asyncio
import functools
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
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
from ai_gateway.budget import Reservation
from ai_gateway.call_log import CallLogEntry
from ai_gateway.gate import Limiter
from ai_gateway.main import GatewayDeps, create_app
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptMessage, PromptRegistry
from ai_gateway.query_dictionary import QueryDictionary
from ai_gateway.rate_limit import RateLimiter
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
    """테스트 프로필. `task`로 event_judge 설정을, `extra`로 최상위 항목을 바꾼다.

    Registry의 taskType마다 설정이 있어야 해서 event_judge_escalate·track_attribute·
    object_question·state_compare도 같은 설정으로 둔다. risk_forecast는 규칙 점수 predictor다.
    """
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
            "adapters": {
                "fake": {"kind": "mock"},
                "rules": {
                    "kind": "predictor",
                    "predictor": {"horizons_sec": [600, 1800], "saturation_s": 1800},
                },
            },
            "tasks": {
                **{
                    name: event_judge
                    for name in (
                        "event_judge",
                        "event_judge_escalate",
                        "track_attribute",
                        "object_question",
                    )
                },
                # 이미지가 없는 taskType이지만 프로필 상한은 0보다 커야 한다(SPEC-027).
                "source_summary": {**event_judge, "max_images": 1},
                "state_compare": {**event_judge, "min_images": 2, "max_images": 2},
                "risk_forecast": {
                    **event_judge,
                    "adapter": "rules",
                    "model": "rule-forecast-v1",
                    "max_images": 1,
                },
            },
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
    timeout_s: float = 0
    temperature: float | None = None


@dataclass
class FakeAdapter:
    """정해 둔 응답을 돌려주고 받은 요청을 기록한다."""

    reply: str | Exception = ""
    calls: list[AdapterCall] = field(default_factory=list)
    # 응답 전에 기다리는 시간. 마감이 지나는 상황을 만든다(SPEC-021).
    delay_s: float = 0.0

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
        self.calls.append(
            AdapterCall(
                model, message, images, max_output_tokens, traceparent, timeout_s, temperature
            )
        )
        await asyncio.sleep(self.delay_s)
        if isinstance(self.reply, Exception):
            raise self.reply
        return Completion(text=self.reply, input_tokens=100, output_tokens=20)


@dataclass(frozen=True)
class EmbedCall:
    model: str
    texts: list[str]
    traceparent: str | None
    timeout_s: float = 0


@dataclass
class FakeEmbedder:
    """`vectors`를 그대로 돌려주고(없으면 차원 4의 결정적 벡터) 받은 요청을 기록한다."""

    reply: list[list[float]] | Exception | None = None
    calls: list[EmbedCall] = field(default_factory=list)
    delay_s: float = 0.0

    async def embed(
        self,
        *,
        model: str,
        texts: list[str],
        timeout_s: float,
        max_retries: int,
        traceparent: str | None,
    ) -> Embeddings:
        self.calls.append(EmbedCall(model, texts, traceparent, timeout_s))
        await asyncio.sleep(self.delay_s)
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


class CountingStorage(InMemoryStorage):
    """읽은 횟수를 세는 메모리 저장소. 저장소를 부르지 않았는지 확인한다(SPEC-021 R-2)."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def get_bytes(self, key: str) -> bytes:
        self.reads += 1
        return super().get_bytes(key)


@dataclass
class Gateway:
    client: httpx.AsyncClient
    adapter: FakeAdapter
    call_log: RecordingCallLog
    storage: CountingStorage
    cache: "MemoryCache"
    budget: "MemoryBudget"


@asynccontextmanager
async def gateway(
    registry_dir: Path,
    gateway_profile: GatewayProfile | None = None,
    adapter: FakeAdapter | None = None,
    prepare: Callable[[CountingStorage], None] | None = None,
    ready: Callable[[], bool] = lambda: True,
    adapters: dict[str, VlmAdapter] | None = None,
    call_log: CallLogWriter | None = None,
    encoder: ImageTextEncoder | None = None,
    embedders: dict[str, TextEmbeddingAdapter] | None = None,
    cache: "MemoryCache | None" = None,
    budget: "MemoryBudget | None" = None,
    limiter: Limiter | None = None,
) -> AsyncIterator[Gateway]:
    """가짜 Adapter·메모리 저장소·기록용 호출 로그로 앱을 띄운다."""
    storage = CountingStorage()
    for i, key in enumerate(IMAGE_KEYS):
        storage.put_bytes(key, f"jpeg-{i}".encode(), "image/jpeg")
    if prepare is not None:
        prepare(storage)
    fake = adapter or FakeAdapter()
    recording = RecordingCallLog()
    memory_cache = cache if cache is not None else MemoryCache()
    memory_budget = budget if budget is not None else MemoryBudget()
    deps = GatewayDeps(
        profile=gateway_profile or profile(),
        prompts=PromptRegistry.load(registry_dir),
        dictionary=QueryDictionary.load(registry_dir),
        storage=storage,
        adapters=adapters if adapters is not None else {"fake": fake},
        call_log=call_log if call_log is not None else recording,
        is_ready=ready,
        encoder=encoder,
        embedders=embedders or {},
        cache=memory_cache,
        budget=memory_budget,
        limiter=limiter or RateLimiter(gateway_profile or profile()),
    )

    async def build() -> GatewayDeps:
        return deps

    app = create_app(build)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-gateway") as client:
            yield Gateway(client, fake, recording, storage, memory_cache, memory_budget)


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
    client: httpx.AsyncClient,
    body: dict[str, object],
    caller: str | None = CALLER,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {"X-Caller": caller} if caller is not None else {}
    return await client.post("/v1/analyze", json=body, headers={**sent, **(headers or {})})


def mock_encoder() -> MockEncoder:
    return MockEncoder(ENCODER_ID, 8)


async def post_embed(
    client: httpx.AsyncClient,
    body: dict[str, object],
    caller: str | None = CALLER,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {"X-Caller": caller} if caller is not None else {}
    return await client.post("/v1/embed", json=body, headers={**sent, **(headers or {})})


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


async def post_parse(
    client: httpx.AsyncClient,
    query: object,
    caller: str | None = CALLER,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    sent = {"X-Caller": caller} if caller is not None else {}
    return await client.post(
        "/v1/parse", json={"query": query}, headers={**sent, **(headers or {})}
    )


@dataclass
class MemoryCache:
    """메모리 결과 캐시. `front=False`면 Redis가 없는 것처럼 잠금을 못 건다."""

    values: dict[str, dict[str, object]] = field(default_factory=dict)
    locks: set[str] = field(default_factory=set)
    sources: dict[str, UUID | None] = field(default_factory=dict)
    front: bool = True
    # 다른 요청이 잠근 호출을 기다리면 이 값을 받는다(None이면 그 호출이 실패한 것).
    awaited: dict[str, object] | None = None
    # wait_for가 결과를 돌려주기 전에 기다리는 시간(동시 호출 합치기 대기, SPEC-021 R-1)
    wait_s: float = 0.0
    # get·try_lock·wait_for를 부른 횟수(SPEC-021 R-2)
    calls: int = 0

    async def get(self, key: str) -> dict[str, object] | None:
        self.calls += 1
        return self.values.get(key)

    async def put(
        self, key: str, task_type: str, source_id: UUID | None, value: dict[str, object]
    ) -> None:
        self.values[key] = value
        self.sources[key] = source_id

    async def try_lock(self, key: str, ttl_s: float) -> bool | None:
        self.calls += 1
        if not self.front:
            return None
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    async def release(self, key: str) -> None:
        self.locks.discard(key)

    async def wait_for(self, key: str, timeout_s: float) -> dict[str, object] | None:
        self.calls += 1
        # 실제 구현처럼 제한 시간이 지나면 결과 없이 돌아온다.
        await asyncio.sleep(min(self.wait_s, timeout_s))
        return self.awaited if self.wait_s <= timeout_s else None

    async def delete_source(self, source_id: UUID) -> int:
        keys = [k for k, s in self.sources.items() if s == source_id]
        for key in keys:
            self.values.pop(key, None)
            del self.sources[key]
        return len(keys)


@dataclass
class MemoryBudget:
    """메모리 예산. 한도는 범위(호출 서비스, "*")별이고 없으면 무제한이다."""

    limits: dict[str, Decimal] = field(default_factory=dict)
    spent: dict[str, Decimal] = field(default_factory=dict)
    # reserve를 부른 횟수(SPEC-021 R-2)
    reserves: int = 0

    async def reserve(self, caller: str, cost: Decimal) -> Reservation | None:
        self.reserves += 1
        scopes = (caller, "*")
        for scope in scopes:
            limit = self.limits.get(scope)
            if limit is not None and self.spent.get(scope, Decimal(0)) + cost > limit:
                return None
        for scope in scopes:
            self.spent[scope] = self.spent.get(scope, Decimal(0)) + cost
        return Reservation(date(2030, 1, 1), scopes, cost)

    async def settle(self, reservation: Reservation, actual: Decimal) -> None:
        for scope in reservation.scopes:
            self.spent[scope] += actual - reservation.cost

    async def release(self, reservation: Reservation) -> None:
        await self.settle(reservation, Decimal(0))
