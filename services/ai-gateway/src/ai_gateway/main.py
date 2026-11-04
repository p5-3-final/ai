"""앱 조립과 진입점(`uvicorn --factory ai_gateway.main:production_app`).

기동할 때 의존성을 한 번 만들고(lifespan) 검증에 실패하면 앱이 뜨지 않는다(SPEC-009 R-14).
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from vlm_common.health import HealthState
from vlm_common.logging import configure_logging
from vlm_common.profile import ProfileError, load_profile
from vlm_encoders.base import ImageTextEncoder
from vlm_encoders.mock import MockEncoder
from vlm_encoders.open_clip import OpenClipEncoder
from vlm_encoders.weights import fetch_encoder_weights
from vlm_storage.s3 import S3Storage, StorageSettings
from vlm_storage.storage import ObjectStorage

from ai_gateway.adapters.base import Adapter, TextEmbeddingAdapter, VlmAdapter
from ai_gateway.adapters.mock import MockAdapter
from ai_gateway.adapters.openai import OpenAiAdapter
from ai_gateway.analyze import AnalyzeService, CallLogWriter
from ai_gateway.api import router
from ai_gateway.call_log import CallLog
from ai_gateway.deletion import start_deletion_worker
from ai_gateway.embed import EmbedService
from ai_gateway.images import ImageReader
from ai_gateway.problems import install_problem_handlers
from ai_gateway.profile import GatewayProfile, check_profile
from ai_gateway.prompts import PromptRegistry
from ai_gateway.settings import GatewaySettings

SERVICE_NAME = "ai-gateway"


async def _nothing() -> None:
    return None


@dataclass(frozen=True)
class GatewayDeps:
    """앱이 쓰는 외부 자원. 운영은 설정에서 만들고, 테스트는 가짜로 바꿔 넣는다."""

    profile: GatewayProfile
    prompts: PromptRegistry
    storage: ObjectStorage
    adapters: dict[str, VlmAdapter]
    call_log: CallLogWriter
    # 기동 검증·DB 확인이 끝나고 video.deleted 구독을 시작했는지(R-23).
    is_ready: Callable[[], bool]
    close: Callable[[], Awaitable[None]] = field(default=_nothing)
    # 프로필에 [encoder]가 없으면 None이다(SPEC-011 R-14).
    encoder: ImageTextEncoder | None = None
    embedders: dict[str, TextEmbeddingAdapter] = field(default_factory=dict)


def create_app(build: Callable[[], Awaitable[GatewayDeps]]) -> FastAPI:
    """`build`는 기동할 때 한 번 불린다. 실패하면 앱이 뜨지 않는다(R-14)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        deps = await build()
        app.state.analyze = AnalyzeService(
            deps.profile, deps.prompts, ImageReader(deps.storage), deps.adapters, deps.call_log
        )
        app.state.embed = EmbedService(
            deps.profile, deps.encoder, ImageReader(deps.storage), deps.embedders, deps.call_log
        )
        app.state.is_ready = deps.is_ready
        try:
            yield
        finally:
            await deps.close()

    app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
    app.include_router(router)
    install_problem_handlers(app)

    @app.get("/health", include_in_schema=False)
    async def health() -> Response:
        # 요청을 처리할 수 있으면 살아 있다. 공급자·DB 상태를 넣으면 장애 때 모든 Pod가 재시작된다.
        return Response(status_code=200)

    @app.get("/ready", include_in_schema=False)
    async def ready() -> Response:
        return Response(status_code=200 if app.state.is_ready() else 503)

    return app


def check_tasks(profile: GatewayProfile, prompts: PromptRegistry) -> None:
    """프로필의 taskType과 Prompt Registry의 taskType이 같은지 확인한다.

    Raises:
        ProfileError: 프롬프트가 없는 taskType 설정이 있거나, 설정이 없는 프롬프트가 있을 때.
    """
    configured, prompted = set(profile.tasks), set(prompts.task_types())
    if configured - prompted:
        raise ProfileError(f"tasks without prompts: {sorted(configured - prompted)}")
    if prompted - configured:
        # 이미지 상한을 모르는 taskType 요청을 받지 않도록, 모델이 없어도 설정은 둬야 한다.
        raise ProfileError(f"prompts without task settings: {sorted(prompted - configured)}")


def load_encoder(
    profile: GatewayProfile, models: ObjectStorage | None, cache_dir: Path
) -> ImageTextEncoder | None:
    """프로필의 [encoder]로 인코더를 한 번 로드한다. 블로킹이라 기동 단계에서 스레드로 부른다.

    Args:
        profile: 검증한 프로필.
        models: 모델 버킷. 설정이 없으면 None이다.
        cache_dir: 가중치 로컬 캐시 폴더.

    Raises:
        ProfileError: open_clip 인코더인데 모델 버킷 설정이 없을 때.
        EncoderLoadError: manifest·가중치를 받지 못했거나, 해시가 다르거나, manifest·로드한 모델이
            프로필과 맞지 않을 때(SPEC-011 R-16, R-17).
    """
    encoder = profile.encoder
    if encoder is None:
        return None
    if encoder.kind == "mock":
        return MockEncoder(encoder.model_id, encoder.dimension)
    # open_clip 프로필 검증에서 manifest 경로·해시가 있음을 확인했다.
    if models is None or encoder.manifest is None or encoder.manifest_sha256 is None:
        raise ProfileError("open_clip encoder requires the models bucket setting")
    weights = fetch_encoder_weights(
        models,
        encoder.manifest,
        encoder.manifest_sha256,
        encoder.model_id,
        encoder.dimension,
        cache_dir,
    )
    return OpenClipEncoder.load(encoder.model_id, weights, encoder.dimension)


def build_adapters(profile: GatewayProfile, api_key: str | None) -> dict[str, Adapter]:
    """프로필의 Adapter마다 구현을 만든다. 종류는 프로필 검증에서 이미 확인했다."""
    text = profile.embed.embed_text
    adapters: dict[str, Adapter] = {}
    for name, adapter in profile.adapters.items():
        if adapter.kind == "mock":
            dimension = text.dimension if text and text.adapter == name and text.dimension else 8
            adapters[name] = MockAdapter(embedding_dimension=dimension)
        else:
            adapters[name] = OpenAiAdapter(
                api_key=api_key,
                base_url=str(adapter.base_url) if adapter.base_url else None,
                compatible=adapter.kind == "openai_compatible",
            )
    return adapters


async def build_production(settings: GatewaySettings) -> GatewayDeps:
    """설정으로 운영 의존성을 만든다. 무엇이든 잘못되면 예외로 기동을 멈춘다(R-14)."""
    api_key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    profile = load_profile(settings.profile_path, GatewayProfile)
    check_profile(profile, allow_mock=settings.allow_mock, has_openai_key=api_key is not None)
    prompts = PromptRegistry.load(settings.registry_dir)
    check_tasks(profile, prompts)
    models = (
        S3Storage(StorageSettings(bucket=settings.models_bucket))
        if settings.models_bucket
        else None
    )
    # 가중치 받기·로드는 블로킹이다. 끝나야 lifespan이 끝나서 그 전에는 요청을 받지 않는다(R-25).
    encoder = await asyncio.to_thread(load_encoder, profile, models, settings.model_cache_dir)
    database_url = settings.database_url.get_secret_value()
    engine = create_async_engine(database_url)
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    health = HealthState()
    worker = start_deletion_worker(
        settings.kafka_bootstrap, settings.group_id, database_url, health
    )

    async def close() -> None:
        worker.stop()
        await engine.dispose()

    adapters = build_adapters(profile, api_key)
    return GatewayDeps(
        profile=profile,
        prompts=prompts,
        storage=S3Storage(StorageSettings()),
        adapters=dict[str, VlmAdapter](adapters),
        call_log=CallLog(engine),
        is_ready=health.is_ready,
        close=close,
        encoder=encoder,
        embedders=dict[str, TextEmbeddingAdapter](adapters),
    )


def production_app() -> FastAPI:
    """uvicorn `--factory`용 진입점. 바인드 주소·포트는 실행 인자로 준다(Dockerfile)."""
    settings = GatewaySettings()
    configure_logging(SERVICE_NAME, settings.log_level)

    async def build() -> GatewayDeps:
        return await build_production(settings)

    return create_app(build)
