"""`/v1/analyze` 유스케이스(SPEC-009 R-1~R-11, R-17)."""

import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ai_gateway.adapters.base import VlmAdapter
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.errors import InvalidRequestError, TooManyImagesError, UnknownTaskTypeError
from ai_gateway.images import ImageReader
from ai_gateway.output import parse_output
from ai_gateway.profile import GatewayProfile
from ai_gateway.prompts import PromptRegistry

type JsonObject = dict[str, object]


class CallLogWriter(Protocol):
    async def save(self, entry: CallLogEntry) -> None: ...


@dataclass(frozen=True, slots=True)
class AnalyzeCommand:
    caller: str
    task_type: str
    source_id: UUID
    image_keys: list[str]
    variables: JsonObject
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    call_id: UUID
    task_type: str
    model_id: str | None
    prompt_version: str
    output: JsonObject


class AnalyzeService:
    def __init__(
        self,
        profile: GatewayProfile,
        prompts: PromptRegistry,
        images: ImageReader,
        adapters: dict[str, VlmAdapter],
        call_log: CallLogWriter,
    ) -> None:
        self._profile = profile
        self._prompts = prompts
        self._images = images
        self._adapters = adapters
        self._call_log = call_log

    async def analyze(self, command: AnalyzeCommand) -> AnalyzeResult:
        started = time.perf_counter()
        task = self._prompts.get(command.task_type)
        if task is None:
            raise UnknownTaskTypeError(f"unknown taskType: {command.task_type}")
        errors = task.input_errors(command.variables)
        if errors:
            raise InvalidRequestError(
                "variables do not match the input schema",
                [_field_error("variables", e) for e in errors],
            )
        settings = self._profile.tasks[command.task_type]
        if len(command.image_keys) > settings.max_images:
            raise TooManyImagesError(f"at most {settings.max_images} images are allowed")
        images = await self._images.read(command.image_keys)

        call_id = new_call_id()
        if settings.adapter is None or settings.model is None:
            raise NotImplementedError("SPEC-009 R-8")
        completion = await self._adapters[settings.adapter].complete(
            model=settings.model,
            message=task.render(command.variables),
            images=images,
            max_output_tokens=settings.max_output_tokens,
            timeout_s=settings.timeout_s,
            max_retries=settings.max_retries,
            traceparent=command.traceparent,
        )
        parsed = parse_output(task, completion.text, command.variables)
        status: CallStatus = "success" if parsed.valid else "invalid_output"
        await self._call_log.save(
            CallLogEntry(
                call_id=call_id,
                caller=command.caller,
                source_id=command.source_id,
                task_type=command.task_type,
                adapter=settings.adapter,
                model_id=settings.model,
                prompt_version=task.version,
                status=status,
                error_code=None,
                latency_ms=int((time.perf_counter() - started) * 1000),
                image_keys=command.image_keys,
                request_config={
                    "maxOutputTokens": settings.max_output_tokens,
                    "timeoutS": settings.timeout_s,
                    "imageCount": len(images),
                },
                output=parsed.output,
                raw_output=completion.text,
                input_tokens=completion.input_tokens,
                output_tokens=completion.output_tokens,
                trace_id=None,
            )
        )
        return AnalyzeResult(
            call_id, command.task_type, settings.model, task.version, parsed.output
        )


def _field_error(prefix: str, error: str) -> tuple[str, str]:
    path, _, reason = error.partition(": ")
    return (prefix if path == "<root>" else f"{prefix}/{path}", reason)
