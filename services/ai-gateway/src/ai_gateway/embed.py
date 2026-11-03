"""`/v1/embed` 유스케이스(SPEC-011)."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from uuid import UUID

from vlm_encoders.base import ImageDecodeError, ImageTextEncoder, Vector

from ai_gateway.adapters.base import (
    ProviderFailedError,
    ProviderUnavailableError,
    TextEmbeddingAdapter,
)
from ai_gateway.analyze import CallLogWriter
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.errors import (
    CallerForbiddenError,
    ImageUnreadableError,
    InvalidRequestError,
    ModelNotConfiguredError,
    TooManyInputsError,
    UnknownTaskTypeError,
)
from ai_gateway.images import ImageReader, QueryImagePolicy
from ai_gateway.profile import EncoderProfile, GatewayProfile

type JsonObject = dict[str, object]

logger = logging.getLogger(__name__)

_TEXT_TASKS = frozenset({"embed_visual_text", "embed_text"})
_TASKS = _TEXT_TASKS | {"embed_image"}


@dataclass(frozen=True, slots=True)
class EmbedCommand:
    caller: str
    task_type: str
    texts: list[str] | None
    image_keys: list[str] | None
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class EmbedResult:
    call_id: UUID
    task_type: str
    model_id: str
    dimension: int
    vectors: list[Vector]


@dataclass(frozen=True, slots=True)
class _Produced:
    vectors: list[Vector]
    input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class _Plan:
    """taskType 하나를 실행하는 데 필요한 프로필 값과 실행 함수."""

    adapter: str
    model_id: str
    dimension: int
    max_inputs: int
    max_text_chars: int | None
    request_config: JsonObject
    run: Callable[[list[str]], Awaitable[_Produced]]


class EmbedService:
    def __init__(
        self,
        profile: GatewayProfile,
        encoder: ImageTextEncoder | None,
        images: ImageReader,
        embedders: dict[str, TextEmbeddingAdapter],
        call_log: CallLogWriter,
    ) -> None:
        self._profile = profile
        self._encoder = encoder
        self._images = images
        self._embedders = embedders
        self._call_log = call_log

    async def embed(self, command: EmbedCommand) -> EmbedResult:
        call = _Call(command, time.perf_counter())
        # /v1/analyze와 같은 허용 목록이다(SPEC-011 R-13).
        if command.caller not in self._profile.callers.allowed:
            raise CallerForbiddenError("caller is not allowed")
        if command.task_type not in _TASKS:
            raise UnknownTaskTypeError(f"unknown taskType: {command.task_type}")
        inputs = _inputs(command)
        plan = self._plan(command)
        if plan is None:
            await self._record(call.entry("unavailable"))
            raise ModelNotConfiguredError(f"no model is configured for {command.task_type}")
        if len(inputs) > plan.max_inputs:
            raise TooManyInputsError(f"at most {plan.max_inputs} inputs are allowed")
        if plan.max_text_chars is not None and any(len(t) > plan.max_text_chars for t in inputs):
            raise InvalidRequestError(f"each text must be at most {plan.max_text_chars} characters")

        try:
            produced = await plan.run(inputs)
        except (ProviderUnavailableError, ProviderFailedError) as e:
            status: CallStatus = (
                "unavailable" if isinstance(e, ProviderUnavailableError) else "failed"
            )
            await self._record(call.entry(status, plan, error_code=e.code))
            raise
        vectors = _checked_unit_vectors(produced.vectors, len(inputs), plan.dimension)
        if vectors is None:
            error = ProviderFailedError("provider returned vectors that do not match the profile")
            await self._record(
                call.entry("invalid_output", plan, error_code=error.code, produced=produced)
            )
            raise error
        await self._record(call.entry("success", plan, produced=produced))
        return EmbedResult(
            call_id=call.call_id,
            task_type=command.task_type,
            model_id=plan.model_id,
            dimension=plan.dimension,
            vectors=vectors,
        )

    def _plan(self, command: EmbedCommand) -> _Plan | None:
        """프로필에서 taskType의 실행 방법을 만든다. 모델이 없으면 None이다(SPEC-011 R-14)."""
        embed = self._profile.embed
        encoder, encoder_profile = self._encoder, self._profile.encoder
        if command.task_type == "embed_text":
            text = embed.embed_text
            if text is None or text.adapter is None or text.model is None or not text.dimension:
                return None
            adapter, model = self._embedders[text.adapter], text.model

            async def run_text(texts: list[str]) -> _Produced:
                result = await adapter.embed(
                    model=model,
                    texts=texts,
                    timeout_s=text.timeout_s,
                    max_retries=text.max_retries,
                    traceparent=command.traceparent,
                )
                return _Produced(result.vectors, result.input_tokens)

            return _Plan(
                adapter=text.adapter,
                model_id=model,
                dimension=text.dimension,
                max_inputs=text.max_inputs,
                max_text_chars=text.max_text_chars,
                request_config={"timeoutS": text.timeout_s, "maxRetries": text.max_retries},
                run=run_text,
            )
        # 모델 설정의 기준은 프로필이다. 인코더는 프로필의 [encoder]로 기동할 때 만든다.
        if encoder is None or encoder_profile is None:
            return None
        if command.task_type == "embed_image":
            image = embed.embed_image
            if image is None:
                return None
            policy = QueryImagePolicy(image.allowed_patterns)

            async def run_image(keys: list[str]) -> _Produced:
                policy.check(keys)
                images = await self._images.read(keys)
                try:
                    # CPU를 많이 써서 요청 처리 루프를 막지 않게 스레드에서 돌린다(SPEC-011 NFR-3).
                    vectors = await asyncio.to_thread(
                        encoder.encode_images, [i.data for i in images]
                    )
                except ImageDecodeError as e:
                    raise ImageUnreadableError(str(e)) from e
                return _Produced(vectors)

            return _encoder_plan(encoder_profile, image.max_inputs, None, run_image)
        visual = embed.embed_visual_text
        if visual is None:
            return None

        async def run_visual(texts: list[str]) -> _Produced:
            return _Produced(await asyncio.to_thread(encoder.encode_texts, texts))

        return _encoder_plan(encoder_profile, visual.max_inputs, visual.max_text_chars, run_visual)

    async def _record(self, entry: CallLogEntry) -> None:
        await self._call_log.save(entry)


def _encoder_plan(
    encoder: EncoderProfile,
    max_inputs: int,
    max_text_chars: int | None,
    run: Callable[[list[str]], Awaitable[_Produced]],
) -> _Plan:
    return _Plan(
        adapter=encoder.kind,
        model_id=encoder.model_id,
        dimension=encoder.dimension,
        max_inputs=max_inputs,
        max_text_chars=max_text_chars,
        request_config={},
        run=run,
    )


def _inputs(command: EmbedCommand) -> list[str]:
    """taskType에 맞는 입력 목록을 꺼낸다(SPEC-011 R-7)."""
    wants_texts = command.task_type in _TEXT_TASKS
    given, other = (
        (command.texts, command.image_keys) if wants_texts else (command.image_keys, command.texts)
    )
    kind = "texts" if wants_texts else "images"
    if other is not None or not given:
        raise InvalidRequestError(f"{command.task_type} requires a non-empty {kind} list only")
    if wants_texts and not all(given):
        raise InvalidRequestError("texts must not be empty strings")
    return given


def _checked_unit_vectors(vectors: list[Vector], count: int, dimension: int) -> list[Vector] | None:
    """개수·차원이 맞으면 길이 1로 정규화해 돌려준다(SPEC-011 R-4, R-19). 아니면 None이다."""
    if len(vectors) != count or any(len(v) != dimension for v in vectors):
        return None
    norms = [math.hypot(*v) for v in vectors]
    if not all(norms):
        return None
    return [[x / n for x in v] for v, n in zip(vectors, norms, strict=True)]


@dataclass(frozen=True, slots=True)
class _Call:
    command: EmbedCommand
    started: float
    call_id: UUID = field(default_factory=new_call_id)

    def entry(
        self,
        status: CallStatus,
        plan: _Plan | None = None,
        *,
        error_code: str | None = None,
        produced: _Produced | None = None,
    ) -> CallLogEntry:
        inputs = self.command.texts or self.command.image_keys or []
        return CallLogEntry(
            call_id=self.call_id,
            caller=self.command.caller,
            source_id=None,
            task_type=self.command.task_type,
            adapter=plan.adapter if plan else None,
            model_id=plan.model_id if plan else None,
            prompt_version=None,
            status=status,
            error_code=error_code,
            latency_ms=int((time.perf_counter() - self.started) * 1000),
            image_keys=self.command.image_keys or [],
            request_config=plan.request_config if plan else {},
            output={"dimension": plan.dimension} if plan and status == "success" else None,
            raw_output=None,
            input_tokens=produced.input_tokens if produced else None,
            output_tokens=None,
            trace_id=self.command.traceparent.split("-")[1] if self.command.traceparent else None,
            input_count=len(inputs),
        )
