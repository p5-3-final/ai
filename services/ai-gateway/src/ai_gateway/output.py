"""모델 출력 해석: 응답 글에서 JSON 객체를 꺼내 출력 스키마로 검증한다(SPEC-009 R-5~R-8).

모델은 지시를 어기고 코드 블록이나 설명을 덧붙이기도 한다. 그런 응답에서도 JSON 객체는
꺼내 쓰고, 꺼내지 못하거나 스키마와 맞지 않으면 결과를 지어내지 않고 `unknown`으로 둔다.
"""

import json
from collections.abc import Iterator
from dataclasses import dataclass

from ai_gateway.prompts import PromptTask

type JsonObject = dict[str, object]

_DECODER = json.JSONDecoder()


@dataclass(frozen=True, slots=True)
class ParsedOutput:
    """검증된 출력. `valid`가 False면 `output`은 대체 결과이고 `errors`에 이유가 있다."""

    output: JsonObject
    valid: bool
    errors: list[str]


def parse_output(task: PromptTask, raw: str, variables: JsonObject) -> ParsedOutput:
    """응답 원문에서 출력을 꺼내 검증한다.

    Args:
        task: 출력 스키마를 가진 taskType.
        raw: 모델 응답 원문.
        variables: 요청의 입력 변수. 대체 결과를 만들 때 쓴다.

    Returns:
        스키마에 맞으면 그 객체, 아니면 `unknown` 대체 결과.
    """
    candidates = list(_json_objects(raw))
    if not candidates:
        return _invalid(task, variables, ["no JSON object in model output"])
    errors: list[str] = []
    for candidate in candidates:
        found = task.output_errors(candidate)
        if not found:
            return ParsedOutput(output=candidate, valid=True, errors=[])
        errors = errors or found
    return _invalid(task, variables, errors)


def _invalid(task: PromptTask, variables: JsonObject, errors: list[str]) -> ParsedOutput:
    return ParsedOutput(output=task.fallback(variables, "unknown"), valid=False, errors=errors)


def _json_objects(raw: str) -> Iterator[JsonObject]:
    """본문 안의 `{` 위치마다 JSON 객체를 읽어 본다. 코드 블록·앞뒤 설명이 있어도 찾는다."""
    for start in (i for i, ch in enumerate(raw) if ch == "{"):
        try:
            value, _ = _DECODER.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value
