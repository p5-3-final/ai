"""실제 응답을 docs 계약 `openapi/ai-gateway.yaml`과 대조한다(SPEC-009 계약 우선)."""

import io
import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
import yaml
from ai_gateway.adapters.base import ProviderUnavailableError
from gateway_support import (
    CALLER,
    FakeAdapter,
    FakeEmbedder,
    analyze_body,
    embed_settings,
    gateway,
    mock_encoder,
    output_example,
    post_analyze,
    post_embed,
    profile,
)
from jsonschema import Draft202012Validator
from PIL import Image
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema

BASE = "https://contracts.vlm/openapi/"
RESPONSE = "ai-gateway.yaml#/components/schemas/AnalyzeResponse"
PROBLEM = "_common.yaml#/components/schemas/ProblemDetail"


def _load(contracts_dir: Path, name: str) -> Mapping[str, object]:
    loaded = yaml.safe_load((contracts_dir / "openapi" / name).read_text(encoding="utf-8"))
    assert isinstance(loaded, Mapping)
    return loaded


def _at(node: object, *keys: str) -> object:
    for key in keys:
        assert isinstance(node, Mapping), key
        node = node[key]
    return node


@pytest.fixture(scope="module")
def registry(contracts_dir: Path) -> Registry[Schema]:
    return Registry().with_resources(
        (BASE + name, Resource.from_contents(_load(contracts_dir, name), DRAFT202012))
        for name in ("ai-gateway.yaml", "_common.yaml")
    )


def _assert_valid(registry: Registry[Schema], schema: str, body: object) -> None:
    validator = Draft202012Validator({"$ref": BASE + schema}, registry=registry)
    assert [e.message for e in validator.iter_errors(body)] == []


def _assert_matches_contract(registry: Registry[Schema], response: httpx.Response) -> None:
    if response.status_code == 200:
        _assert_valid(registry, RESPONSE, response.json())
    else:
        assert response.headers["content-type"].startswith("application/problem+json")
        _assert_valid(registry, PROBLEM, response.json())


async def test_contract_request_example_is_accepted(
    contracts_dir: Path, registry_dir: Path, registry: Registry[Schema]
) -> None:
    example = _at(
        _load(contracts_dir, "ai-gateway.yaml"),
        "paths",
        "/v1/analyze",
        "post",
        "requestBody",
        "content",
        "application/json",
        "example",
    )
    assert isinstance(example, dict)
    adapter = FakeAdapter(reply=json.dumps(output_example(registry_dir)))

    async with gateway(registry_dir, adapter=adapter) as gw:
        response = await post_analyze(gw.client, example)

    assert response.status_code == 200
    _assert_matches_contract(registry, response)


async def test_contract_response_example_matches_schemas(
    contracts_dir: Path, registry: Registry[Schema]
) -> None:
    example = _at(
        _load(contracts_dir, "ai-gateway.yaml"),
        "paths",
        "/v1/analyze",
        "post",
        "responses",
        "200",
        "content",
        "application/json",
        "example",
    )

    _assert_valid(registry, RESPONSE, example)


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("confirmed", 200),
        ("unknown", 200),
        ("unverified", 200),
        ("invalid", 400),
        ("caller", 403),
        ("unavailable", 503),
    ],
)
async def test_contract_responses_match_contract(
    registry_dir: Path, registry: Registry[Schema], case: str, status: int
) -> None:
    reply: str | Exception = json.dumps(output_example(registry_dir))
    gateway_profile = profile()
    body = analyze_body()
    caller: str | None = CALLER
    if case == "unknown":
        reply = "판단 불가"
    elif case == "unverified":
        gateway_profile = profile(adapter=None, model=None)
    elif case == "invalid":
        body = analyze_body(images=None)
    elif case == "caller":
        caller = None
    elif case == "unavailable":
        reply = ProviderUnavailableError("timeout")

    async with gateway(registry_dir, gateway_profile, adapter=FakeAdapter(reply=reply)) as gw:
        response = await post_analyze(gw.client, body, caller=caller)

    assert response.status_code == status
    _assert_matches_contract(registry, response)


EMBED_RESPONSE = "ai-gateway.yaml#/components/schemas/EmbedResponse"
EMBED_REQUEST = "ai-gateway.yaml#/components/schemas/EmbedRequest"
# 계약의 요청 예시가 쓰는 검색용 이미지 경로
QUERY_IMAGE_EXAMPLE = "queries/0192f0d2-9a8b-7c6d-8e4f-5a6b7c8d9e0f/query.jpg"


def _embed_examples(contracts_dir: Path) -> dict[str, dict[str, object]]:
    examples = _at(
        _load(contracts_dir, "ai-gateway.yaml"),
        "paths",
        "/v1/embed",
        "post",
        "requestBody",
        "content",
        "application/json",
        "examples",
    )
    assert isinstance(examples, dict)
    return {name: example["value"] for name, example in examples.items()}


@pytest.mark.parametrize("name", ["visualText", "image", "text"])
async def test_spec011_contract_embed_request_examples_are_accepted(
    contracts_dir: Path, registry_dir: Path, registry: Registry[Schema], name: str
) -> None:
    """[SPEC-011] 계약의 요청 예시를 그대로 받아 계약 형식으로 응답한다."""
    example = _embed_examples(contracts_dir)[name]
    _assert_valid(registry, EMBED_REQUEST, example)

    async with gateway(
        registry_dir,
        profile(embed_settings()),
        encoder=mock_encoder(),
        embedders={"fake": FakeEmbedder()},
        prepare=lambda s: s.put_bytes(QUERY_IMAGE_EXAMPLE, _query_jpeg(), "image/jpeg"),
    ) as gw:
        response = await post_embed(gw.client, example)

    assert response.status_code == 200, response.text
    _assert_valid(registry, EMBED_RESPONSE, response.json())


def test_spec011_contract_embed_response_example_matches_schema(
    contracts_dir: Path, registry: Registry[Schema]
) -> None:
    example = _at(
        _load(contracts_dir, "ai-gateway.yaml"),
        "paths",
        "/v1/embed",
        "post",
        "responses",
        "200",
        "content",
        "application/json",
        "example",
    )

    _assert_valid(registry, EMBED_RESPONSE, example)


@pytest.mark.parametrize(
    ("body", "settings", "status"),
    [
        ({"taskType": "embed_visual_text", "texts": ["a"] * 9}, None, 400),
        ({"taskType": "embed_image", "images": [{"storageKey": "sources/x/original"}]}, None, 403),
        ({"taskType": "embed_text", "texts": ["a"]}, {"encoder": None, "embed": {}}, 503),
    ],
)
async def test_spec011_contract_embed_errors_are_problem_details(
    registry_dir: Path,
    registry: Registry[Schema],
    body: dict[str, object],
    settings: dict[str, object] | None,
    status: int,
) -> None:
    """[SPEC-011] 오류는 계약의 ProblemDetail 형식이다."""
    async with gateway(
        registry_dir, profile(settings or embed_settings()), encoder=mock_encoder()
    ) as gw:
        response = await post_embed(gw.client, body)

    assert response.status_code == status
    _assert_matches_contract(registry, response)


def _query_jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "JPEG")
    return buf.getvalue()
