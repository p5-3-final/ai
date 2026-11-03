"""앱 조립: 기동할 때 의존성을 만들고(lifespan) 라우터를 붙인다."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI
from vlm_storage.storage import ObjectStorage

from ai_gateway.adapters.base import VlmAdapter
from ai_gateway.analyze import AnalyzeService, CallLogWriter
from ai_gateway.api import router
from ai_gateway.images import ImageReader
from ai_gateway.problems import install_problem_handlers
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptRegistry


@dataclass(frozen=True)
class GatewayDeps:
    """앱이 쓰는 외부 자원. 운영은 설정에서 만들고, 테스트는 가짜로 바꿔 넣는다."""

    profile: GatewayProfile
    prompts: PromptRegistry
    storage: ObjectStorage
    adapters: dict[str, VlmAdapter]
    call_log: CallLogWriter


def create_app(build: Callable[[], Awaitable[GatewayDeps]]) -> FastAPI:
    """`build`는 기동할 때 한 번 불린다. 실패하면 앱이 뜨지 않는다(SPEC-009 R-14)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        deps = await build()
        app.state.analyze = AnalyzeService(
            deps.profile, deps.prompts, ImageReader(deps.storage), deps.adapters, deps.call_log
        )
        yield

    app = FastAPI(title="ai-gateway", lifespan=lifespan)
    app.include_router(router)
    install_problem_handlers(app)
    return app
