"""`/v1/analyze` 유스케이스(SPEC-009 R-1~R-11, R-17)."""

import time
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ai_gateway.adapters.base import Completion, VlmAdapter
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.errors import InvalidRequestError, TooManyImagesError, UnknownTaskTypeError
from ai_gateway.images import ImageReader
from ai_gateway.output import fallback_output, parse_output
from ai_gateway.profile import GatewayProfile, TaskProfile
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

        call = _Call(command, task.version, settings, started)
        if settings.adapter is None or settings.model is None:
            # SPEC-009 R-8: 모델이 없으면 결과를 지어내지 않고 unverified로 돌려준다.
            output = fallback_output(command.task_type, command.variables, "unverified")
            await self._call_log.save(call.entry("unavailable", output=output))
            return call.result(output)

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
        await self._call_log.save(
            call.entry(
                "success" if parsed.valid else "invalid_output",
                output=parsed.output,
                completion=completion,
            )
        )
        return call.result(parsed.output)


@dataclass(frozen=True, slots=True)
class _Call:
    """호출 한 건의 공통 값. 결과마다 호출 로그와 응답을 같은 값으로 만든다."""

    command: AnalyzeCommand
    prompt_version: str
    settings: TaskProfile
    started: float
    call_id: UUID = field(default_factory=new_call_id)

    def entry(
        self,
        status: CallStatus,
        *,
        output: JsonObject | None,
        completion: Completion | None = None,
        error_code: str | None = None,
    ) -> CallLogEntry:
        return CallLogEntry(
            call_id=self.call_id,
            caller=self.command.caller,
            source_id=self.command.source_id,
            task_type=self.command.task_type,
            adapter=self.settings.adapter,
            model_id=self.settings.model,
            prompt_version=self.prompt_version,
            status=status,
            error_code=error_code,
            latency_ms=int((time.perf_counter() - self.started) * 1000),
            image_keys=self.command.image_keys,
            request_config={
                "maxOutputTokens": self.settings.max_output_tokens,
                "timeoutS": self.settings.timeout_s,
                "imageCount": len(self.command.image_keys),
            },
            output=output,
            raw_output=completion.text if completion else None,
            input_tokens=completion.input_tokens if completion else None,
            output_tokens=completion.output_tokens if completion else None,
            trace_id=None,
        )

    def result(self, output: JsonObject) -> AnalyzeResult:
        return AnalyzeResult(
            call_id=self.call_id,
            task_type=self.command.task_type,
            model_id=self.settings.model,
            prompt_version=self.prompt_version,
            output=output,
        )


def _field_error(prefix: str, error: str) -> tuple[str, str]:
    path, _, reason = error.partition(": ")
    return (prefix if path == "<root>" else f"{prefix}/{path}", reason)
