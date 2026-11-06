"""클라이언트가 계약에서 생성한 모델만 쓰는지 확인한다(SPEC-021 R-6)."""

import ast
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
import yaml
from ac_support import contracts_dir
from pydantic import BaseModel
from vlm_ai_client import models

SRC = Path(__file__).resolve().parents[1] / "src" / "vlm_ai_client"


def _defines_model(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id in {"BaseModel", "RootModel"} for b in node.bases)
    ]


def test_spec021_r6_no_hand_written_models_outside_generated_module() -> None:
    """[SPEC-021 R-6] 요청·응답 모델은 생성 모듈(models.py)에만 있다."""
    hand_written = {
        str(path.relative_to(SRC)): names
        for path in SRC.rglob("*.py")
        if path.name != "models.py" and (names := _defines_model(path))
    }

    assert hand_written == {}


def _operation(path: str) -> Mapping[str, object]:
    loaded = yaml.safe_load(
        (contracts_dir() / "openapi" / "ai-gateway.yaml").read_text(encoding="utf-8")
    )
    operation = loaded["paths"][path]["post"]
    assert isinstance(operation, Mapping)
    return operation


def _examples(content: Mapping[str, object]) -> list[object]:
    media = content["application/json"]
    assert isinstance(media, Mapping)
    if "example" in media:
        return [media["example"]]
    examples = media["examples"]
    assert isinstance(examples, Mapping)
    return [e["value"] for e in examples.values()]


def _analyze_response_example() -> dict[str, object]:
    responses = _operation("/v1/analyze")["responses"]
    assert isinstance(responses, Mapping)
    example = _examples(responses["200"]["content"])[0]
    assert isinstance(example, dict)
    return example


_CASES = [
    ("/v1/analyze", models.AnalyzeRequest, models.AnalyzeResponse),
    ("/v1/embed", models.EmbedRequest, models.EmbedResponse),
    ("/v1/parse", models.ParseRequest, models.ParseResponse),
]


@pytest.mark.parametrize(("path", "request_model", "response_model"), _CASES)
def test_spec021_r6_contract_examples_round_trip(
    path: str, request_model: type[BaseModel], response_model: type[BaseModel]
) -> None:
    """[SPEC-021 R-6] 계약의 요청·응답 예시가 생성 모델로 바뀌지 않고 왕복한다."""
    operation = _operation(path)
    request_body = operation["requestBody"]
    responses = operation["responses"]
    assert isinstance(request_body, Mapping)
    assert isinstance(responses, Mapping)
    pairs = [(request_model, e) for e in _examples(request_body["content"])] + [
        (response_model, e) for e in _examples(responses["200"]["content"])
    ]

    for model, example in pairs:
        parsed = model.model_validate(example)
        dumped = json.loads(parsed.model_dump_json(by_alias=True, exclude_unset=True))
        assert dumped == example, model.__name__


def test_spec021_r6_open_task_type_accepts_unknown_value() -> None:
    """[SPEC-021 R-6] taskType은 열린 목록(ADR-0014)이라 모르는 값도 받는다."""
    example = _analyze_response_example()

    parsed = models.AnalyzeResponse.model_validate({**example, "taskType": "added_later"})

    assert parsed.task_type == "added_later"


def test_spec021_r6_response_ignores_fields_added_later() -> None:
    """[SPEC-021 R-6] 계약은 응답 필드 추가를 허용하므로 모르는 필드를 무시한다."""
    example = _analyze_response_example()

    parsed = models.AnalyzeResponse.model_validate({**example, "addedLater": 1})

    assert "addedLater" not in parsed.model_dump(by_alias=True)


def test_spec021_r6_request_rejects_unknown_fields() -> None:
    """[SPEC-021 R-6] 요청은 계약의 additionalProperties: false를 따른다."""
    with pytest.raises(ValueError, match="extra"):
        models.ParseRequest.model_validate({"query": "x", "addedLater": 1})
