"""docs 계약 원본(service-contracts) 위치와 JSON Schema 검증.

서비스 테스트와 로컬 E2E가 같은 방식으로 계약을 검증하도록 한 곳에 둔다.
모든 스키마를 각자의 `$id`로 등록하므로 스키마끼리의 상대 경로 참조가 그대로 풀린다.
"""

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema

# libs/testing/src/vlm_testing/contracts.py → 루트 구조(project/repos/ai)의 project 폴더
_ROOT_DEPTH = 6


# 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포의 service-contracts/를 읽는다.
# CI는 service-contracts.lock 버전을 받아 CONTRACTS_DIR로 준다(docs ADR-0013).
def _default_contracts_dir() -> Path:
    parents = Path(__file__).resolve().parents
    # 컨테이너처럼 루트 구조가 아니면 기본값이 없다. 그때는 CONTRACTS_DIR이 꼭 있어야 한다.
    if len(parents) > _ROOT_DEPTH:
        return parents[_ROOT_DEPTH] / "service-contracts"
    return Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


def contracts_dir() -> Path:
    """docs 계약 원본 폴더(환경변수 `CONTRACTS_DIR`).

    원본이 없으면 계약 검증이 빠진 채 통과하므로 건너뛰지 않고 실패시킨다.
    """
    path = _ContractSettings().contracts_dir
    if not path.is_dir():
        raise FileNotFoundError(f"계약을 찾을 수 없다: {path} (CONTRACTS_DIR 확인)")
    return path


@cache
def _registry(root: Path) -> Registry[Schema]:
    resources = []
    for schema in sorted(root.rglob("*.schema.json")):
        raw = json.loads(schema.read_text(encoding="utf-8"))
        if "$id" in raw:
            resources.append((str(raw["$id"]), Resource.from_contents(raw, DRAFT202012)))
    return Registry().with_resources(resources)


def schema_errors(schema: str, body: object) -> list[str]:
    """`body`(JSON 값)를 계약 스키마로 검증한 오류 메시지(`[경로]: 메시지`). 없으면 빈 목록이다.

    Args:
        schema: 스키마 `$id`(https://…) 또는 계약 폴더 기준 상대 경로(`events/x.schema.json`).
        body: 검증할 JSON 값.
    """
    root = contracts_dir()
    schema_id = schema
    if not schema.startswith(("http://", "https://")):
        schema_id = str(json.loads((root / schema).read_text(encoding="utf-8"))["$id"])
    validator = Draft202012Validator({"$ref": schema_id}, registry=_registry(root))
    return [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(body)]


def model_errors(model: BaseModel, schema: str) -> list[str]:
    """Pydantic 모델을 별칭 JSON으로 바꿔 `schema`로 검증한다."""
    return schema_errors(schema, json.loads(model.model_dump_json(by_alias=True)))


def event_errors(event: BaseModel, event_type: str | None = None) -> list[str]:
    """발행한 이벤트를 `events/<eventType>.schema.json`으로 검증한다.

    Args:
        event: 이벤트 모델. `event_type`을 주지 않으면 모델의 `event_type` 값을 쓴다.
        event_type: 스키마 이름(토픽 이름과 같다).
    """
    body = json.loads(event.model_dump_json(by_alias=True))
    name = event_type if event_type is not None else str(body["eventType"])
    return schema_errors(f"events/{name}.schema.json", body)


def body_event_errors(body: dict[str, object]) -> list[str]:
    """이미 JSON인 이벤트 본문을 `eventType` 값의 스키마로 검증한다(Kafka에서 읽은 메시지 등)."""
    return schema_errors(f"events/{body['eventType']}.schema.json", body)
