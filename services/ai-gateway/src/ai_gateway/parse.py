"""`/v1/parse` 유스케이스(SPEC-012).

질의 사전이 먼저다. 사전에 맞는 한국어 표현이 없을 때만 영어 구문이나 LLM(`query_parse`)을 쓰고,
그래도 해석하지 못하면 오류 대신 산업 CCTV 기본 대상 `person`으로 제한한다. 검색은 VLM 장애와
상관없이 이어져야 하기 때문이다(docs overview "AI Gateway" 가용성).
"""

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal
from uuid import UUID

from ai_gateway.adapters.base import (
    Completion,
    ProviderUnavailableError,
    VlmAdapter,
)
from ai_gateway.analyze import CallLogWriter
from ai_gateway.call_log import CallLogEntry, CallStatus, new_call_id
from ai_gateway.errors import CallerForbiddenError, InvalidRequestError
from ai_gateway.gate import GateCall, ModelGate, Produced, cache_key
from ai_gateway.output import ParsedOutput, parse_output
from ai_gateway.profile import QUERY_PARSE_TASK, GatewayProfile
from ai_gateway.prompts import PromptRegistry
from ai_gateway.query_dictionary import QueryDictionary, Route

type JsonObject = dict[str, object]
type ParseMethod = Literal["industrial-rule", "english-phrase", "llm", "safe-fallback"]

logger = logging.getLogger(__name__)

# 산업 CCTV 질의 대부분이 찾는 대상. 해석하지 못한 질의를 이 대상으로 제한한다(R-9, R-12~R-14).
_FALLBACK_TARGET = "person"
# 표준 질의 문자열 형식의 버전. 형식을 바꾸면 올려 예전 재사용 키와 섞이지 않게 한다(R-18).
_CANONICAL_FORMAT = "qv1"
# 같은 문장이면 같은 대상어가 나오게 한다(R-10).
_TEMPERATURE = 0.0


@dataclass(frozen=True, slots=True)
class ParseCommand:
    caller: str
    query: str
    traceparent: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    call_id: UUID
    route: Route
    targets: tuple[str, ...]
    event_types: tuple[str, ...]
    visual_query: str
    parse_method: ParseMethod
    rationale: str
    dictionary_version: str
    prompt_version: str | None
    taxonomy_version: str | None
    canonical_query: str


@dataclass(frozen=True, slots=True)
class _Parsed:
    """해석 결과와 호출 로그에 남길 LLM 호출 정보."""

    method: ParseMethod
    targets: tuple[str, ...]
    event_types: tuple[str, ...]
    rationale: str
    status: CallStatus = "success"
    # LLM을 불렀을 때만 있다(R-27).
    llm_called: bool = False
    completion: Completion | None = None
    error_code: str | None = None
    # LLM 경로만 채운다(SPEC-016 R-19).
    cache_hit: bool | None = None
    reserved_usd: Decimal | None = None
    settled_usd: Decimal | None = None


class ParseService:
    def __init__(
        self,
        profile: GatewayProfile,
        dictionary: QueryDictionary,
        prompts: PromptRegistry,
        adapters: dict[str, VlmAdapter],
        call_log: CallLogWriter,
        gate: ModelGate,
    ) -> None:
        self._profile = profile
        self._gate = gate
        self._settings = profile.parse
        self._dictionary = dictionary
        self._task = prompts.get(QUERY_PARSE_TASK)
        self._adapters = adapters
        self._call_log = call_log

    async def parse(self, command: ParseCommand) -> ParseResult:
        started = time.perf_counter()
        # /v1/analyze와 같은 허용 목록이다(SPEC-012 R-22).
        if command.caller not in self._profile.callers.allowed:
            raise CallerForbiddenError("caller is not allowed")
        if len(command.query) > self._settings.max_query_chars:
            raise InvalidRequestError(
                f"query must be at most {self._settings.max_query_chars} characters"
            )
        normalized = self._dictionary.normalize(command.query)
        if not normalized:
            raise InvalidRequestError("query is empty after normalization")

        # route는 해석 방법과 상관없이 사전으로만 정한다(R-15).
        route = self._dictionary.route(normalized)
        parsed = await self._interpret(normalized, command)
        call = _Call(command, started)
        prompt_version = self._prompt_version() if parsed.method == "llm" else None
        result = ParseResult(
            call_id=call.call_id,
            route=route,
            targets=parsed.targets,
            event_types=parsed.event_types,
            visual_query=self._dictionary.visual_query(parsed.targets),
            parse_method=parsed.method,
            rationale=parsed.rationale,
            dictionary_version=self._dictionary.version,
            prompt_version=prompt_version,
            taxonomy_version=self._dictionary.taxonomy_version,
            canonical_query=self._canonical(route, parsed, prompt_version),
        )
        await self._record(call.entry(result, parsed, self._llm_values(parsed)))
        return result

    async def _interpret(self, normalized: str, command: ParseCommand) -> _Parsed:
        # 사전 해석은 Adapter를 부르지 않아 공급자 상태와 상관없이 된다(R-26).
        found = self._dictionary.match(normalized)
        if found is not None:
            return _Parsed(
                method="industrial-rule",
                targets=found.targets,
                event_types=found.event_types,
                rationale=f"industrial-rule: 사전 표현 {', '.join(found.phrases)}",
            )
        phrase = self._dictionary.english_phrase(normalized)
        if phrase:
            return _Parsed(
                method="english-phrase",
                targets=(phrase,),
                event_types=(),
                rationale="english-phrase: 불용어를 지운 영어 구문",
            )
        if phrase == "":
            return _fallback("영어 구문에 대상어가 없음")
        return await self._ask_llm(normalized, command)

    async def _ask_llm(self, normalized: str, command: ParseCommand) -> _Parsed:
        settings, task = self._settings, self._task
        if settings.adapter is None or settings.model is None or task is None:
            return _fallback("query_parse 모델 미설정", status="unavailable")
        adapter, model, prompt = settings.adapter, settings.model, task
        variables: JsonObject = {"query": normalized}

        async def invoke() -> Produced[tuple[Completion, ParsedOutput]]:
            completion = await self._adapters[adapter].complete(
                model=model,
                message=prompt.render(variables),
                images=[],
                max_output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
                max_retries=settings.max_retries,
                traceparent=command.traceparent,
                temperature=_TEMPERATURE,
            )
            parsed = parse_output(prompt, completion.text, variables)
            return Produced(
                (completion, parsed),
                completion.input_tokens,
                completion.output_tokens,
                parsed.output if parsed.valid else None,
            )

        gated = await self._gate.run(
            GateCall(
                caller=command.caller,
                task_type=QUERY_PARSE_TASK,
                adapter=adapter,
                model=model,
                # 정규화 문장이 같으면 표현이 달라도 같은 키다(SPEC-016 R-2).
                cache_key=cache_key(QUERY_PARSE_TASK, adapter, model, prompt.version, variables),
                source_id=None,
                input_tokens=settings.estimated_input_tokens,
                output_tokens=settings.max_output_tokens,
                timeout_s=settings.timeout_s,
            ),
            invoke,
        )
        reserved, settled = gated.reserved_usd, gated.settled_usd
        if gated.kind == "cached" and gated.cached is not None:
            return _llm(gated.cached, cache_hit=True, reserved_usd=reserved, settled_usd=settled)
        if gated.kind == "budget_exceeded":
            # 예산을 넘으면 모델 미설정과 같은 결과다(SPEC-016 R-11).
            return _fallback("query_parse 예산 초과", status="budget_exceeded", cache_hit=False)
        if gated.kind == "failed" and gated.error is not None:
            error = gated.error
            # 일시 오류와 그 밖의 거부를 나눠 남긴다(R-13).
            status: CallStatus = (
                "unavailable" if isinstance(error, ProviderUnavailableError) else "failed"
            )
            return _fallback(
                "query_parse 공급자 오류",
                status,
                llm_called=True,
                error=getattr(error, "log_code", None) or getattr(error, "code", None),
                cache_hit=False,
                reserved_usd=reserved,
                settled_usd=settled,
            )
        if gated.value is None:
            raise RuntimeError("model gate returned no result")
        completion, parsed = gated.value
        if not parsed.valid:
            return _fallback(
                "query_parse 출력 검증 실패",
                status="invalid_output",
                llm_called=True,
                completion=completion,
                cache_hit=False,
                reserved_usd=reserved,
                settled_usd=settled,
            )
        return _llm(
            parsed.output,
            completion=completion,
            cache_hit=False,
            reserved_usd=reserved,
            settled_usd=settled,
        )

    def _prompt_version(self) -> str | None:
        return self._task.version if self._task else None

    def _canonical(self, route: Route, parsed: _Parsed, prompt_version: str | None) -> str:
        """같은 값이면 같고 하나라도 다르면 다른 문자열(R-18).

        대상어·이벤트 유형에는 계약 형식상 `|`와 `,`가 없어 이어 붙여도 경계가 섞이지 않는다.
        """
        parts = [
            route,
            ",".join(parsed.targets),
            ",".join(parsed.event_types),
            parsed.method,
            self._dictionary.version,
            self._dictionary.taxonomy_version or "",
        ]
        if parsed.method == "llm" and prompt_version is not None:
            parts.append(prompt_version)
        return f"{_CANONICAL_FORMAT}:{'|'.join(parts)}"

    def _llm_values(self, parsed: _Parsed) -> "_LlmValues":
        if not parsed.llm_called:
            return _LlmValues()
        return _LlmValues(
            adapter=self._settings.adapter,
            model_id=self._settings.model,
            prompt_version=self._prompt_version(),
            request_config={
                "maxOutputTokens": self._settings.max_output_tokens,
                "timeoutS": self._settings.timeout_s,
            },
        )

    async def _record(self, entry: CallLogEntry) -> None:
        await self._call_log.save(entry)
        # 검색 문장·대상어·모델 원문은 남기지 않는다. 영어 구문 대상어는 검색 문장과 같다(NFR-3).
        logger.info(
            "parse completed",
            extra={
                "call_id": str(entry.call_id),
                "caller": entry.caller,
                "task_type": entry.task_type,
                "model_id": entry.model_id,
                "prompt_version": entry.prompt_version,
                "taxonomy_version": entry.taxonomy_version,
                "parse_method": (entry.output or {}).get("parseMethod"),
                "dictionary_version": (entry.output or {}).get("dictionaryVersion"),
                "status": entry.status,
                "error_code": entry.error_code,
                "latency_ms": entry.latency_ms,
                "input_tokens": entry.input_tokens,
                "trace_id": entry.trace_id,
            },
        )


@dataclass(frozen=True, slots=True)
class _LlmValues:
    """LLM을 불렀을 때만 호출 로그에 남기는 값(R-27)."""

    adapter: str | None = None
    model_id: str | None = None
    prompt_version: str | None = None
    request_config: JsonObject = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Call:
    command: ParseCommand
    started: float
    call_id: UUID = field(default_factory=new_call_id)

    def entry(self, result: ParseResult, parsed: _Parsed, llm: _LlmValues) -> CallLogEntry:
        completion = parsed.completion
        return CallLogEntry(
            call_id=self.call_id,
            caller=self.command.caller,
            source_id=None,
            task_type=QUERY_PARSE_TASK,
            adapter=llm.adapter,
            model_id=llm.model_id,
            prompt_version=llm.prompt_version,
            taxonomy_version=result.taxonomy_version,
            status=parsed.status,
            error_code=parsed.error_code,
            latency_ms=int((time.perf_counter() - self.started) * 1000),
            image_keys=[],
            request_config=llm.request_config,
            # 대상어는 넣지 않는다. 영어 구문이면 검색 문장 원문과 거의 같다(R-27).
            output={
                "parseMethod": result.parse_method,
                "dictionaryVersion": result.dictionary_version,
                "route": result.route,
                "eventTypes": list(result.event_types),
            },
            # 모델 원문에도 검색 문장이 되풀이될 수 있어 남기지 않는다(R-27).
            raw_output=None,
            input_tokens=completion.input_tokens if completion else None,
            output_tokens=completion.output_tokens if completion else None,
            trace_id=self.command.traceparent.split("-")[1] if self.command.traceparent else None,
            cache_hit=parsed.cache_hit,
            reserved_cost_usd=parsed.reserved_usd,
            settled_cost_usd=parsed.settled_usd,
        )


def _llm(
    output: JsonObject,
    *,
    completion: Completion | None = None,
    cache_hit: bool,
    reserved_usd: Decimal | None,
    settled_usd: Decimal | None,
) -> _Parsed:
    return _Parsed(
        method="llm",
        targets=tuple(_strings(output["targets"])),
        event_types=tuple(_strings(output["eventTypes"])),
        # LLM 설명문은 넣지 않는다. 모델 원문이 응답으로 새지 않게 한다(R-17).
        rationale="llm: query_parse 출력",
        llm_called=True,
        completion=completion,
        cache_hit=cache_hit,
        reserved_usd=reserved_usd,
        settled_usd=settled_usd,
    )


def _fallback(
    reason: str,
    status: CallStatus = "success",
    *,
    llm_called: bool = False,
    completion: Completion | None = None,
    error: str | None = None,
    cache_hit: bool | None = None,
    reserved_usd: Decimal | None = None,
    settled_usd: Decimal | None = None,
) -> _Parsed:
    return _Parsed(
        method="safe-fallback",
        targets=(_FALLBACK_TARGET,),
        event_types=(),
        rationale=f"safe-fallback: {reason}, 대상어를 {_FALLBACK_TARGET}으로 제한",
        status=status,
        llm_called=llm_called,
        completion=completion,
        error_code=error,
        cache_hit=cache_hit,
        reserved_usd=reserved_usd,
        settled_usd=settled_usd,
    )


def _strings(value: object) -> list[str]:
    # 출력 스키마 검증을 통과한 값이라 문자열 목록이다.
    return [str(v) for v in value] if isinstance(value, list) else []
