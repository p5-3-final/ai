"""HTTP 계층: 요청·응답 모델(계약 `ai-gateway.yaml`)과 오류 응답."""

import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ai_gateway.analyze import AnalyzeCommand, AnalyzeService
from ai_gateway.embed import EmbedCommand, EmbedService
from ai_gateway.parse import ParseCommand, ParseService

router = APIRouter()

_TRACEPARENT = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


class _Body(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class ImageRef(_Body):
    storage_key: str = Field(min_length=1, max_length=1024)
    pts_sec: float | None = Field(default=None, ge=0)


class AnalyzeRequest(_Body):
    # 모르는 taskType도 형식 오류가 아니라 AI_UNKNOWN_TASK_TYPE으로 알리려고 enum으로 막지 않는다.
    task_type: str
    source_id: UUID
    images: list[ImageRef]
    variables: dict[str, object]


class AnalyzeResponse(_Body):
    call_id: UUID
    task_type: str
    model_id: str | None
    prompt_version: str
    taxonomy_version: str | None
    output: dict[str, object]


def _service(request: Request) -> AnalyzeService:
    service: AnalyzeService = request.app.state.analyze
    return service


@router.post("/v1/analyze", response_model_by_alias=True)
async def analyze(
    body: AnalyzeRequest,
    service: Annotated[AnalyzeService, Depends(_service)],
    x_caller: Annotated[str | None, Header()] = None,
    traceparent: Annotated[str | None, Header()] = None,
) -> AnalyzeResponse:
    result = await service.analyze(
        AnalyzeCommand(
            caller=x_caller or "",
            task_type=body.task_type,
            source_id=body.source_id,
            image_keys=[i.storage_key for i in body.images],
            variables=body.variables,
            traceparent=_valid_traceparent(traceparent),
        )
    )
    return AnalyzeResponse(
        call_id=result.call_id,
        task_type=result.task_type,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        taxonomy_version=result.taxonomy_version,
        output=result.output,
    )


def _valid_traceparent(value: str | None) -> str | None:
    # 형식이 틀린 추적 헤더 때문에 분석을 거절하지 않는다. 없는 것으로 본다.
    return value if value and _TRACEPARENT.fullmatch(value) else None


class EmbedImageRef(_Body):
    storage_key: str = Field(min_length=1, max_length=1024)


class EmbedRequest(_Body):
    # 모르는 taskType도 형식 오류가 아니라 AI_UNKNOWN_TASK_TYPE으로 알리려고 enum으로 막지 않는다.
    task_type: str
    texts: list[str] | None = None
    images: list[EmbedImageRef] | None = None


class EmbedResponse(_Body):
    call_id: UUID
    task_type: str
    model_id: str
    dimension: int
    vectors: list[list[float]]


def _embed_service(request: Request) -> EmbedService:
    service: EmbedService = request.app.state.embed
    return service


@router.post("/v1/embed", response_model_by_alias=True)
async def embed(
    body: EmbedRequest,
    service: Annotated[EmbedService, Depends(_embed_service)],
    x_caller: Annotated[str | None, Header()] = None,
    traceparent: Annotated[str | None, Header()] = None,
) -> EmbedResponse:
    result = await service.embed(
        EmbedCommand(
            caller=x_caller or "",
            task_type=body.task_type,
            texts=body.texts,
            image_keys=[i.storage_key for i in body.images] if body.images is not None else None,
            traceparent=_valid_traceparent(traceparent),
        )
    )
    return EmbedResponse(
        call_id=result.call_id,
        task_type=result.task_type,
        model_id=result.model_id,
        dimension=result.dimension,
        vectors=result.vectors,
    )


class ParseRequest(_Body):
    query: str = Field(min_length=1)


class ParseResponse(_Body):
    call_id: UUID
    route: str
    targets: list[str]
    event_types: list[str]
    visual_query: str
    parse_method: str
    rationale: str
    dictionary_version: str
    prompt_version: str | None
    taxonomy_version: str | None
    canonical_query: str


def _parse_service(request: Request) -> ParseService:
    service: ParseService = request.app.state.parse
    return service


@router.post("/v1/parse", response_model_by_alias=True)
async def parse(
    body: ParseRequest,
    service: Annotated[ParseService, Depends(_parse_service)],
    x_caller: Annotated[str | None, Header()] = None,
    traceparent: Annotated[str | None, Header()] = None,
) -> ParseResponse:
    result = await service.parse(
        ParseCommand(
            caller=x_caller or "",
            query=body.query,
            traceparent=_valid_traceparent(traceparent),
        )
    )
    return ParseResponse(
        call_id=result.call_id,
        route=result.route,
        targets=list(result.targets),
        event_types=list(result.event_types),
        visual_query=result.visual_query,
        parse_method=result.parse_method,
        rationale=result.rationale,
        dictionary_version=result.dictionary_version,
        prompt_version=result.prompt_version,
        taxonomy_version=result.taxonomy_version,
        canonical_query=result.canonical_query,
    )
