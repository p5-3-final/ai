"""API 테스트 공통 준비물: 가짜 Adapter·호출 로그와 앱 조립."""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import httpx
from ai_gateway.adapters.base import Completion, ImageInput, VlmAdapter
from ai_gateway.call_log import CallLogEntry
from ai_gateway.main import GatewayDeps, create_app
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptMessage, PromptRegistry
from vlm_storage.memory import InMemoryStorage

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


def profile(**task: object) -> GatewayProfile:
    """테스트 프로필. `task`로 event_judge 설정을 바꾼다."""
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
        }
    )


def output_example(contracts_dir: Path, index: int = 0) -> dict[str, object]:
    schema = json.loads(
        (contracts_dir / "prompts/event_judge/output.schema.json").read_text(encoding="utf-8")
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
    ) -> Completion:
        self.calls.append(AdapterCall(model, message, images, max_output_tokens, traceparent))
        if isinstance(self.reply, Exception):
            raise self.reply
        return Completion(text=self.reply, input_tokens=100, output_tokens=20)


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
    contracts_dir: Path,
    gateway_profile: GatewayProfile | None = None,
    adapter: FakeAdapter | None = None,
    prepare: Callable[[InMemoryStorage], None] | None = None,
    ready: Callable[[], bool] = lambda: True,
) -> AsyncIterator[Gateway]:
    """가짜 Adapter·메모리 저장소·기록용 호출 로그로 앱을 띄운다."""
    storage = InMemoryStorage()
    for i, key in enumerate(IMAGE_KEYS):
        storage.put_bytes(key, f"jpeg-{i}".encode(), "image/jpeg")
    if prepare is not None:
        prepare(storage)
    fake = adapter or FakeAdapter()
    call_log = RecordingCallLog()
    adapters: dict[str, VlmAdapter] = {"fake": fake}
    deps = GatewayDeps(
        profile=gateway_profile or profile(),
        prompts=PromptRegistry.load(contracts_dir),
        storage=storage,
        adapters=adapters,
        call_log=call_log,
        is_ready=ready,
    )

    async def build() -> GatewayDeps:
        return deps

    app = create_app(build)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://ai-gateway") as client:
            yield Gateway(client, fake, call_log, storage)


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
