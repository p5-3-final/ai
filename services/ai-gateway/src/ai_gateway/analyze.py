"""`/v1/analyze` 유스케이스(SPEC-009 R-1~R-11, R-17)."""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from ai_gateway.adapters.base import (
    Completion,
    ProviderUnavailableError,
    VlmAdapter,
)
from ai_gateway.adapters.predictor import Predictor
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.deadline import Deadline, cap
from ai_gateway.errors import (
    CallerForbiddenError,
    InvalidRequestError,
    TooManyImagesError,
    UnknownTaskTypeError,
)
from ai_gateway.gate import GateCall, ModelGate, Produced, cache_key
from ai_gateway.images import ImagePolicy, ImageReader
from ai_gateway.output import ParsedOutput, parse_output
from ai_gateway.profile import GatewayProfile, TaskProfile
from ai_gateway.prompts import PromptRegistry, PromptTask

type JsonObject = dict[str, object]
# 결과가 unverified인 이유(SPEC-016 R-23). 호출자가 재판정 큐를 사유로 나눈다.
type UnverifiedReason = Literal["model_unavailable", "budget_exceeded"]

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
    # 호출자 마감(SPEC-021). 없으면 프로필 제한 시간만 쓴다(R-5).
    deadline: Deadline | None = None


@dataclass(frozen=True, slots=True)
class AnalyzeResult:
    call_id: UUID
    task_type: str
    model_id: str | None
    prompt_version: str
    taxonomy_version: str | None
    output: JsonObject
    unverified_reason: UnverifiedReason | None = None


class AnalyzeService:
    def __init__(
        self,
        profile: GatewayProfile,
        prompts: PromptRegistry,
        images: ImageReader,
        adapters: dict[str, VlmAdapter],
        call_log: CallLogWriter,
        gate: ModelGate,
        predictors: dict[str, Predictor] | None = None,
    ) -> None:
        self._profile = profile
        self._prompts = prompts
        self._images = images
        self._image_policy = ImagePolicy(profile.images.allowed_patterns)
        self._adapters = adapters
        self._call_log = call_log
        self._gate = gate
        self._predictors = predictors or {}

    async def analyze(self, command: AnalyzeCommand) -> AnalyzeResult:
        started = time.perf_counter()
        # 토큰 인증 대신 호출자 이름으로 사용량을 나누고 모르는 호출자를 막는다(SPEC-009 R-16).
        if command.caller not in self._profile.callers.allowed:
            raise CallerForbiddenError("caller is not allowed")
        task = self._prompts.get(command.task_type)
        # [tasks]에 없는 Registry taskType(query_parse)은 /v1/parse 전용이다(SPEC-012).
        if task is None or command.task_type not in self._profile.tasks:
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
        if len(command.image_keys) < settings.min_images:
            message = f"at least {settings.min_images} images are required"
            raise InvalidRequestError(message, [("images", message)])
        self._image_policy.check(command.source_id, command.image_keys)
        images = await self._images.read(command.image_keys)

        call = _Call(command, task.version, task.taxonomy_version, settings, started)
        if settings.adapter is None or settings.model is None:
            # SPEC-009 R-8: 모델이 없으면 결과를 지어내지 않고 unverified로 돌려준다.
            output = task.fallback(command.variables, "unverified")
            await self._record(call.entry("unavailable", output=output))
            return call.result(output, unverified_reason="model_unavailable")

        adapter, model = settings.adapter, settings.model
        predictor = self._predictors.get(adapter)
        if predictor is not None:
            return await self._predict(call, task, predictor, model)

        async def invoke() -> Produced[tuple[Completion, ParsedOutput]]:
            completion = await self._adapters[adapter].complete(
                model=model,
                message=task.render(command.variables),
                images=images,
                max_output_tokens=settings.max_output_tokens,
                # 호출 직전의 남은 시간으로 줄인다(SPEC-021 R-1).
                timeout_s=cap(command.deadline, settings.timeout_s),
                max_retries=settings.max_retries,
                traceparent=command.traceparent,
            )
            parsed = parse_output(task, completion.text, command.variables)
            # 출력 검증을 통과한 결과만 저장한다(SPEC-016 R-3).
            store: JsonObject | None = (
                {
                    "output": parsed.output,
                    "modelId": model,
                    "promptVersion": task.version,
                    "taxonomyVersion": task.taxonomy_version,
                }
                if parsed.valid
                else None
            )
            return Produced(
                (completion, parsed), completion.input_tokens, completion.output_tokens, store
            )

        # 블러 사본을 다시 만들면 같은 경로라도 내용이 달라져 내용으로 비교한다(SPEC-016 R-4).
        content = {
            "images": [hashlib.sha256(i.data).hexdigest() for i in images],
            "variables": command.variables,
        }
        gated = await self._gate.run(
            GateCall(
                caller=command.caller,
                task_type=command.task_type,
                adapter=adapter,
                model=model,
                cache_key=cache_key(command.task_type, adapter, model, task.version, content),
                source_id=command.source_id,
                input_tokens=settings.estimated_input_tokens,
                output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
                deadline=command.deadline,
            ),
            invoke,
        )
        costs = _Costs(gated.reserved_usd, gated.settled_usd)
        if gated.kind == "cached" and gated.cached is not None:
            cached = gated.cached
            output = _object(cached["output"])
            await self._record(call.entry("success", output=output, cache_hit=True, costs=costs))
            return AnalyzeResult(
                call_id=call.call_id,
                task_type=command.task_type,
                model_id=_text(cached.get("modelId")),
                prompt_version=str(cached["promptVersion"]),
                taxonomy_version=_text(cached.get("taxonomyVersion")),
                output=output,
            )
        if gated.kind == "budget_exceeded":
            # 예산을 넘으면 모델 미설정과 같은 결과다(SPEC-016 R-11). 호출자가 이미 처리하는 경로다.
            output = task.fallback(command.variables, "unverified")
            await self._record(call.entry("budget_exceeded", output=output, cache_hit=False))
            return call.result(output, called_model=False, unverified_reason="budget_exceeded")
        if gated.kind == "failed" and gated.error is not None:
            error = gated.error
            # 실패한 호출도 호출자·모델별 장애를 볼 수 있게 남긴다(SPEC-009 R-17~R-19).
            status: CallStatus = (
                "unavailable" if isinstance(error, ProviderUnavailableError) else "failed"
            )
            await self._record(
                call.entry(
                    status,
                    output=None,
                    error_code=getattr(error, "log_code", None) or getattr(error, "code", None),
                    cache_hit=False,
                    costs=costs,
                )
            )
            raise error
        if gated.value is None:
            raise RuntimeError("model gate returned no result")
        completion, parsed = gated.value
        await self._record(
            call.entry(
                "success" if parsed.valid else "invalid_output",
                output=parsed.output,
                completion=completion,
                cache_hit=False,
                costs=costs,
            )
        )
        return call.result(parsed.output)

    async def _predict(
        self, call: "_Call", task: PromptTask, predictor: Predictor, model: str
    ) -> AnalyzeResult:
        """predictor 경로(SPEC-028 R-16). 외부 비용·지연이 없어 결과 캐시·예산·분당 한도를 거치지
        않는다. 출력은 VLM과 같이 출력 스키마로 검증하고 호출 로그를 남긴다."""
        variables = call.command.variables
        output = predictor.predict(model=model, variables=variables)
        if task.output_errors(output):
            # 규칙이 계약과 어긋나면 값을 내보내지 않고 판단할 수 없음으로 둔다(R-5와 같은 처리).
            fallback = task.fallback(variables, "unknown")
            await self._record(call.entry("invalid_output", output=fallback))
            return call.result(fallback)
        await self._record(call.entry("success", output=output))
        return call.result(output)

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
                "taxonomy_version": entry.taxonomy_version,
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
    # 결과의 enum이 어느 분류 체계 버전인지 남긴다(SPEC-009 R-29).
    taxonomy_version: str | None
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
        cache_hit: bool | None = None,
        costs: "_Costs | None" = None,
    ) -> CallLogEntry:
        return CallLogEntry(
            call_id=self.call_id,
            caller=self.command.caller,
            source_id=self.command.source_id,
            task_type=self.command.task_type,
            adapter=self.settings.adapter,
            model_id=self.settings.model,
            prompt_version=self.prompt_version,
            taxonomy_version=self.taxonomy_version,
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
            cache_hit=cache_hit,
            reserved_cost_usd=costs.reserved if costs else None,
            settled_cost_usd=costs.settled if costs else None,
        )

    def result(
        self,
        output: JsonObject,
        *,
        called_model: bool = True,
        unverified_reason: UnverifiedReason | None = None,
    ) -> AnalyzeResult:
        # 모델을 부르지 않은 결과(예산 초과)는 미설정과 같이 모델 ID가 없다(SPEC-016 R-11).
        return AnalyzeResult(
            call_id=self.call_id,
            task_type=self.command.task_type,
            model_id=self.settings.model if called_model else None,
            prompt_version=self.prompt_version,
            taxonomy_version=self.taxonomy_version,
            output=output,
            unverified_reason=unverified_reason,
        )


def _field_error(prefix: str, error: str) -> tuple[str, str]:
    path, _, reason = error.partition(": ")
    return (prefix if path == "<root>" else f"{prefix}/{path}", reason)


@dataclass(frozen=True, slots=True)
class _Costs:
    """호출 로그에 남길 예산 예약·정산 비용(SPEC-016 R-19)."""

    reserved: Decimal | None
    settled: Decimal | None


def _object(value: object) -> JsonObject:
    # 캐시에는 출력 스키마 검증을 통과한 객체만 저장한다(R-3).
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None
