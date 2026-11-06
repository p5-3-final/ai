"""실제 응답을 docs 계약 `openapi/ai-gateway.yaml`과 대조한다(SPEC-009 계약 우선)."""

import io
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import yaml
from ai_gateway.adapters.base import ProviderUnavailableError
from gateway_support import (
    CALLER,
    FakeAdapter,
    FakeEmbedder,
    MemoryBudget,
    analyze_body,
    embed_settings,
    gateway,
    mock_encoder,
    output_example,
    parse_settings,
    post_analyze,
    post_embed,
    post_parse,
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
    openapi = [
        (BASE + name, Resource.from_contents(_load(contracts_dir, name), DRAFT202012))
        for name in ("ai-gateway.yaml", "_common.yaml")
    ]
    # ParseResponse가 분류 체계를 상대 경로로 참조한다(SPEC-012).
    # 분류 체계 안의 참조는 자기 $id로 푼다.
    raw = json.loads(
        (contracts_dir / "taxonomy/v2/taxonomy.schema.json").read_text(encoding="utf-8")
    )
    taxonomy = Resource.from_contents(raw, DRAFT202012)
    return Registry().with_resources(
        [
            *openapi,
            ("https://contracts.vlm/taxonomy/v2/taxonomy.schema.json", taxonomy),
            (str(raw["$id"]), taxonomy),
        ]
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


PARSE_RESPONSE = "ai-gateway.yaml#/components/schemas/ParseResponse"
PARSE_REQUEST = "ai-gateway.yaml#/components/schemas/ParseRequest"


def _parse_operation(contracts_dir: Path, *keys: str) -> object:
    return _at(_load(contracts_dir, "ai-gateway.yaml"), "paths", "/v1/parse", "post", *keys)


async def test_spec012_contract_parse_request_example_is_accepted(
    contracts_dir: Path, registry_dir: Path, registry: Registry[Schema]
) -> None:
    """[SPEC-012 R-1] 계약의 요청 예시를 그대로 받아 계약 형식으로 응답한다."""
    example = _parse_operation(
        contracts_dir, "requestBody", "content", "application/json", "example"
    )
    _assert_valid(registry, PARSE_REQUEST, example)
    assert isinstance(example, dict)

    async with gateway(registry_dir) as gw:
        response = await post_parse(gw.client, example["query"])

    assert response.status_code == 200, response.text
    _assert_valid(registry, PARSE_RESPONSE, response.json())


def test_spec012_contract_parse_response_example_matches_schema(
    contracts_dir: Path, registry: Registry[Schema]
) -> None:
    example = _parse_operation(
        contracts_dir, "responses", "200", "content", "application/json", "example"
    )

    _assert_valid(registry, PARSE_RESPONSE, example)


@pytest.mark.parametrize(
    ("query", "reply", "settings"),
    [
        ("Find a forklift near the worker", "", None),
        ("사다리 위에 올라간", '{"targets": ["ladder"], "eventTypes": ["fall"]}', None),
        ("사다리 위에 올라간", ProviderUnavailableError("down"), None),
        ("사다리 위에 올라간", "", {"adapter": None, "model": None}),
        ("show me", "", None),
    ],
)
async def test_spec012_contract_every_parse_method_matches_contract(
    registry_dir: Path,
    registry: Registry[Schema],
    query: str,
    reply: str | Exception,
    settings: dict[str, object] | None,
) -> None:
    """[SPEC-012 R-1] 해석 방법마다 응답이 계약 형식이다."""
    gateway_profile = profile(extra={"parse": parse_settings(**(settings or {}))})
    async with gateway(registry_dir, gateway_profile, adapter=FakeAdapter(reply=reply)) as gw:
        response = await post_parse(gw.client, query)

    assert response.status_code == 200, response.text
    _assert_valid(registry, PARSE_RESPONSE, response.json())


@pytest.mark.parametrize(
    ("query", "caller", "status"),
    [("찾아줘", CALLER, 400), ("가" * 51, CALLER, 400), ("지게차", "nobody", 403)],
)
async def test_spec012_contract_parse_errors_are_problem_details(
    registry_dir: Path, registry: Registry[Schema], query: str, caller: str, status: int
) -> None:
    """[SPEC-012 R-20~R-22] 오류는 계약의 ProblemDetail 형식이다."""
    async with gateway(registry_dir) as gw:
        response = await post_parse(gw.client, query, caller=caller)

    assert response.status_code == status
    _assert_matches_contract(registry, response)


async def test_spec016_contract_budget_exceeded_is_problem_detail(
    registry_dir: Path, registry: Registry[Schema]
) -> None:
    """[SPEC-016 R-11] embed_text 예산 초과는 계약의 503 ProblemDetail이다."""
    settings = embed_settings()
    priced = profile(
        {**settings, "prices": {"fake": {"fake-embedding": {"input_per_million_usd": 1}}}}
    )
    embed = priced.embed.embed_text
    assert embed is not None
    priced = priced.model_copy(
        update={
            "embed": priced.embed.model_copy(
                update={"embed_text": embed.model_copy(update={"estimated_tokens_per_text": 10})}
            )
        }
    )
    async with gateway(
        registry_dir,
        priced,
        embedders={"fake": FakeEmbedder()},
        budget=MemoryBudget(limits={"*": Decimal(0)}),
    ) as gw:
        response = await post_embed(gw.client, {"taskType": "embed_text", "texts": ["a"]})

    assert response.status_code == 503
    assert response.json()["code"] == "AI_BUDGET_EXCEEDED"
    _assert_matches_contract(registry, response)


async def test_spec016_contract_budget_exceeded_analyze_matches_contract(
    registry_dir: Path, registry: Registry[Schema]
) -> None:
    """[SPEC-016 R-11] analyze 예산 초과는 계약의 200 응답이다."""
    priced = profile(
        extra={
            "prices": {
                "fake": {"fake-vlm": {"input_per_million_usd": 1, "output_per_million_usd": 1}}
            }
        }
    )
    async with gateway(registry_dir, priced, budget=MemoryBudget(limits={"*": Decimal(0)})) as gw:
        response = await post_analyze(gw.client, analyze_body())

    assert response.status_code == 200
    _assert_matches_contract(registry, response)
