"""HTTP 계층: 요청·응답 모델(계약 `ai-gateway.yaml`)과 오류 응답."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from ai_gateway.analyze import AnalyzeCommand, AnalyzeService

router = APIRouter()


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
    output: dict[str, object]


def _service(request: Request) -> AnalyzeService:
    service: AnalyzeService = request.app.state.analyze
    return service


@router.post("/v1/analyze", response_model_by_alias=True)
async def analyze(
    body: AnalyzeRequest,
    service: Annotated[AnalyzeService, Depends(_service)],
    x_caller: Annotated[str | None, Header()] = None,
) -> AnalyzeResponse:
    result = await service.analyze(
        AnalyzeCommand(
            caller=x_caller or "",
            task_type=body.task_type,
            source_id=body.source_id,
            image_keys=[i.storage_key for i in body.images],
            variables=body.variables,
        )
    )
    return AnalyzeResponse(
        call_id=result.call_id,
        task_type=result.task_type,
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        output=result.output,
    )
