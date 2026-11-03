"""실제 응답을 docs 계약 `openapi/ai-gateway.yaml`과 대조한다(SPEC-009 계약 우선)."""

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
    analyze_body,
    gateway,
    output_example,
    post_analyze,
    profile,
)
from jsonschema import Draft202012Validator
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
    contracts_dir: Path, registry: Registry[Schema]
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
    adapter = FakeAdapter(reply=json.dumps(output_example(contracts_dir)))

    async with gateway(contracts_dir, adapter=adapter) as gw:
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
    contracts_dir: Path, registry: Registry[Schema], case: str, status: int
) -> None:
    reply: str | Exception = json.dumps(output_example(contracts_dir))
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

    async with gateway(contracts_dir, gateway_profile, adapter=FakeAdapter(reply=reply)) as gw:
        response = await post_analyze(gw.client, body, caller=caller)

    assert response.status_code == status
    _assert_matches_contract(registry, response)
