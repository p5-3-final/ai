"""`/v1/analyze` 유스케이스(SPEC-009 R-1~R-11, R-17)."""

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ai_gateway.adapters.base import (
    Completion,
    ProviderFailedError,
    ProviderUnavailableError,
    VlmAdapter,
)
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.errors import (
    CallerForbiddenError,
    InvalidRequestError,
    TooManyImagesError,
    UnknownTaskTypeError,
)
from ai_gateway.images import ImagePolicy, ImageReader
from ai_gateway.output import fallback_output, parse_output
from ai_gateway.profile import GatewayProfile, TaskProfile
from ai_gateway.prompts import PromptRegistry

type JsonObject = dict[str, object]

logger = logging.getLogger(__name__)


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
        self._image_policy = ImagePolicy(profile.images.allowed_patterns)
        self._adapters = adapters
        self._call_log = call_log

    async def analyze(self, command: AnalyzeCommand) -> AnalyzeResult:
        started = time.perf_counter()
        # 토큰 인증 대신 호출자 이름으로 사용량을 나누고 모르는 호출자를 막는다(SPEC-009 R-16).
        if command.caller not in self._profile.callers.allowed:
            raise CallerForbiddenError("caller is not allowed")
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
        self._image_policy.check(command.source_id, command.image_keys)
        images = await self._images.read(command.image_keys)

        call = _Call(command, task.version, settings, started)
        if settings.adapter is None or settings.model is None:
            # SPEC-009 R-8: 모델이 없으면 결과를 지어내지 않고 unverified로 돌려준다.
            output = fallback_output(command.task_type, command.variables, "unverified")
            await self._record(call.entry("unavailable", output=output))
            return call.result(output)

        try:
            completion = await self._adapters[settings.adapter].complete(
                model=settings.model,
                message=task.render(command.variables),
                images=images,
                max_output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
                max_retries=settings.max_retries,
                traceparent=command.traceparent,
            )
        except (ProviderUnavailableError, ProviderFailedError) as e:
            # 실패한 호출도 호출자·모델별 장애를 볼 수 있게 남긴다(SPEC-009 R-17~R-19).
            status: CallStatus = (
                "unavailable" if isinstance(e, ProviderUnavailableError) else "failed"
            )
            await self._record(call.entry(status, output=None, error_code=e.code))
            raise
        parsed = parse_output(task, completion.text, command.variables)
        await self._record(
            call.entry(
                "success" if parsed.valid else "invalid_output",
                output=parsed.output,
                completion=completion,
            )
        )
        return call.result(parsed.output)

    async def _record(self, entry: CallLogEntry) -> None:
        await self._call_log.save(entry)
        # 이미지·입력 변수·모델 원문은 개인정보나 영상 내용이 담길 수 있어 로그에 넣지 않는다.
        # 원문은 호출 로그 DB에만 있다(SPEC-009 NFR-3).
        logger.info(
            "analyze completed",
            extra={
                "call_id": str(entry.call_id),
                "caller": entry.caller,
                "source_id": str(entry.source_id),
                "task_type": entry.task_type,
                "model_id": entry.model_id,
                "prompt_version": entry.prompt_version,
                "status": entry.status,
                "error_code": entry.error_code,
                "latency_ms": entry.latency_ms,
                "input_tokens": entry.input_tokens,
                "output_tokens": entry.output_tokens,
                "trace_id": entry.trace_id,
            },
        )


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
            trace_id=self.command.traceparent.split("-")[1] if self.command.traceparent else None,
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
