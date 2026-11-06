"""`/v1/embed` 유스케이스(SPEC-011)."""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from vlm_encoders.base import ImageDecodeError, ImageTextEncoder, Vector
from vlm_encoders.loader import EncoderProfile

from ai_gateway.adapters.base import (
    ProviderFailedError,
    ProviderUnavailableError,
    TextEmbeddingAdapter,
)
from ai_gateway.analyze import CallLogWriter
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.deadline import Deadline, cap
from ai_gateway.errors import (
    BudgetExceededError,
    CallerForbiddenError,
    ImageUnreadableError,
    InvalidRequestError,
    ModelNotConfiguredError,
    TooManyInputsError,
    UnknownTaskTypeError,
)
from ai_gateway.gate import CacheStore, GateCall, ModelGate, Produced, cache_key
from ai_gateway.images import ImageReader, QueryImagePolicy
from ai_gateway.profile import GatewayProfile

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
    # 호출자 마감(SPEC-021). 없으면 프로필 제한 시간만 쓴다(R-5).
    deadline: Deadline | None = None


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
        gate: ModelGate,
        cache: CacheStore,
    ) -> None:
        self._profile = profile
        self._encoder = encoder
        self._images = images
        self._embedders = embedders
        self._call_log = call_log
        self._gate = gate
        self._cache = cache

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
        if command.task_type == "embed_text":
            # 외부 공급자를 부르는 경로만 캐시·예산·분당 한도를 거친다(SPEC-016).
            return await self._embed_text(command, call, plan, inputs)

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

    async def _embed_text(
        self, command: EmbedCommand, call: "_Call", plan: _Plan, inputs: list[str]
    ) -> EmbedResult:
        """문장마다 캐시를 찾고, 없는 문장만 공급자에 보낸다(SPEC-016 R-5)."""
        keys = [
            cache_key(
                command.task_type,
                plan.adapter,
                plan.model_id,
                None,
                {"text": text, "dimension": plan.dimension},
            )
            for text in inputs
        ]
        vectors: list[Vector | None] = [_vector(await self._cache.get(k)) for k in keys]
        missing = [i for i, v in enumerate(vectors) if v is None]
        settings = self._profile.embed.embed_text
        timeout_s = settings.timeout_s if settings else 0.0
        locked: list[str] = []
        try:
            mine = await self._claim(
                missing, keys, vectors, locked, cap(command.deadline, timeout_s)
            )
            if not mine:
                await self._record(call.entry("success", plan, cache_hit=True, costs=_Costs.free()))
                return _result(call, command, plan, vectors)
            texts = [inputs[i] for i in mine]

            async def invoke() -> Produced[_Produced]:
                produced = await plan.run(texts)
                return Produced(produced, produced.input_tokens, None, None)

            per_text = settings.estimated_tokens_per_text if settings else 0
            gated = await self._gate.run(
                GateCall(
                    caller=command.caller,
                    task_type=command.task_type,
                    adapter=plan.adapter,
                    model=plan.model_id,
                    # 문장 묶음 하나에는 키가 없다. 캐시와 동시 호출 합치기는 위에서 문장마다 했다.
                    cache_key=None,
                    source_id=None,
                    input_tokens=per_text * len(texts),
                    output_tokens=0,
                    timeout_s=timeout_s,
                    deadline=command.deadline,
                ),
                invoke,
            )
            costs = _Costs(gated.reserved_usd, gated.settled_usd)
            if gated.kind == "budget_exceeded":
                await self._record(call.entry("budget_exceeded", plan, cache_hit=False))
                raise BudgetExceededError("daily AI budget is exhausted")
            if gated.kind == "failed" and gated.error is not None:
                error = gated.error
                status: CallStatus = (
                    "unavailable" if isinstance(error, ProviderUnavailableError) else "failed"
                )
                code = getattr(error, "log_code", None) or getattr(error, "code", None)
                await self._record(
                    call.entry(status, plan, error_code=code, cache_hit=False, costs=costs)
                )
                raise error
            if gated.value is None:
                raise RuntimeError("model gate returned no result")
            produced = gated.value
            checked = _checked_unit_vectors(produced.vectors, len(texts), plan.dimension)
            if checked is None:
                mismatch = ProviderFailedError(
                    "provider returned vectors that do not match the profile"
                )
                await self._record(
                    call.entry(
                        "invalid_output",
                        plan,
                        error_code=mismatch.code,
                        produced=produced,
                        cache_hit=False,
                        costs=costs,
                    )
                )
                raise mismatch
            for i, vector in zip(mine, checked, strict=True):
                vectors[i] = vector
                await self._cache.put(keys[i], command.task_type, None, {"vector": vector})
            await self._record(
                call.entry("success", plan, produced=produced, cache_hit=False, costs=costs)
            )
            return _result(call, command, plan, vectors)
        finally:
            for key in locked:
                await self._cache.release(key)

    async def _claim(
        self,
        missing: list[int],
        keys: list[str],
        vectors: list[Vector | None],
        locked: list[str],
        timeout_s: float,
    ) -> list[int]:
        """다른 요청이 같은 문장을 보내는 중이면 그 결과를 기다리고, 직접 보낼 문장을 돌려준다.

        SPEC-016 R-7, R-8. 잠근 키는 `locked`에 더해 호출이 끝나면 푼다.
        """
        mine: list[int] = []
        waiting: list[int] = []
        for i in missing:
            got = await self._cache.try_lock(keys[i], timeout_s)
            if got:
                locked.append(keys[i])
            (waiting if got is False else mine).append(i)
        if waiting:
            awaited = await asyncio.gather(
                *(self._cache.wait_for(keys[i], timeout_s) for i in waiting)
            )
            for i, value in zip(waiting, awaited, strict=True):
                vector = _vector(value)
                if vector is None:
                    mine.append(i)
                else:
                    vectors[i] = vector
        return sorted(mine)

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
                    # 호출 직전의 남은 시간으로 줄인다(SPEC-021 R-1).
                    timeout_s=cap(command.deadline, text.timeout_s),
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
        # 검색 문장은 개인정보일 수 있고 벡터는 크기만 커서 로그에 넣지 않는다(SPEC-011 NFR-5).
        logger.info(
            "embed completed",
            extra={
                "call_id": str(entry.call_id),
                "caller": entry.caller,
                "task_type": entry.task_type,
                "model_id": entry.model_id,
                "status": entry.status,
                "error_code": entry.error_code,
                "latency_ms": entry.latency_ms,
                "input_count": entry.input_count,
                "input_tokens": entry.input_tokens,
                "trace_id": entry.trace_id,
            },
        )


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
        cache_hit: bool | None = None,
        costs: "_Costs | None" = None,
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
            taxonomy_version=None,
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
            cache_hit=cache_hit,
            reserved_cost_usd=costs.reserved if costs else None,
            settled_cost_usd=costs.settled if costs else None,
        )


@dataclass(frozen=True, slots=True)
class _Costs:
    """호출 로그에 남길 예산 예약·정산 비용(SPEC-016 R-19)."""

    reserved: Decimal | None
    settled: Decimal | None

    @classmethod
    def free(cls) -> "_Costs":
        # 캐시 적중은 비용 0이다(SPEC-016 R-14).
        return cls(Decimal(0), Decimal(0))


def _vector(value: object) -> Vector | None:
    if not isinstance(value, dict):
        return None
    vector = value.get("vector")
    return [float(x) for x in vector] if isinstance(vector, list) else None


def _result(
    call: _Call, command: EmbedCommand, plan: _Plan, vectors: list[Vector | None]
) -> EmbedResult:
    return EmbedResult(
        call_id=call.call_id,
        task_type=command.task_type,
        model_id=plan.model_id,
        dimension=plan.dimension,
        vectors=[v for v in vectors if v is not None],
    )
