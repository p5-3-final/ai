"""Prompt Registry: docs 계약의 프롬프트·입력·출력 스키마를 읽어 taskType별로 묶는다.

원본은 docs 레포 `docs/contracts/prompts/`다(SPEC-009). 버전 ID는 사람이 붙이지 않고 내용
해시로 계산해, 내용을 바꾸고 버전을 올리지 않는 실수를 없앤다(R-13).
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012, Schema
from vlm_common.errors import VlmError

type JsonObject = dict[str, object]

_PREFIX_FILE = "_prefix.md"
_PROMPT_FILE = "prompt.md"
_INPUT_FILE = "input.schema.json"
_OUTPUT_FILE = "output.schema.json"
# 버전 ID에 쓰는 해시 앞자리 수. 호출 로그·결과에서 사람이 읽을 수 있는 길이로 자른다.
_VERSION_DIGITS = 12


class PromptRegistryError(VlmError):
    """프롬프트 계약을 읽거나 검증하지 못했다. 서비스는 기동에 실패해야 한다(R-14)."""

    code = "AI_PROMPT_INVALID"


@dataclass(frozen=True, slots=True)
class PromptMessage:
    """모델에 보낼 글. 고정 접두부는 입력과 무관해 공급자 프롬프트 캐시를 받는다(R-12)."""

    fixed_prefix: str
    variable_part: str


@dataclass(frozen=True, slots=True, eq=False)
class PromptTask:
    """taskType 하나의 프롬프트와 검증기."""

    task_type: str
    version: str
    fixed_prefix: str
    instruction: str
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator

    def input_errors(self, variables: object) -> list[str]:
        """입력 변수가 입력 스키마와 맞지 않는 이유 목록. 맞으면 빈 목록(R-2)."""
        return _errors(self.input_validator, variables)

    def output_errors(self, output: object) -> list[str]:
        """모델 출력이 출력 스키마와 맞지 않는 이유 목록. 맞으면 빈 목록(R-5)."""
        return _errors(self.output_validator, output)

    def render(self, variables: JsonObject) -> PromptMessage:
        """고정 접두부와, 작업 지시 + 입력 JSON으로 된 가변부를 만든다."""
        payload = json.dumps(variables, ensure_ascii=False, indent=2)
        return PromptMessage(
            fixed_prefix=self.fixed_prefix,
            variable_part=f"{self.instruction}\n\n## 입력\n\n```json\n{payload}\n```\n",
        )


class PromptRegistry:
    """계약 폴더에서 읽은 taskType별 프롬프트."""

    def __init__(self, tasks: dict[str, PromptTask]) -> None:
        self._tasks = tasks

    @classmethod
    def load(cls, contracts_dir: Path) -> "PromptRegistry":
        """`<contracts_dir>/prompts/`의 taskType 폴더를 모두 읽는다.

        Raises:
            PromptRegistryError: 파일이 없거나, JSON이나 스키마가 잘못됐거나, 참조를 풀 수 없을 때.
        """
        prompts_dir = contracts_dir / "prompts"
        prefix = _read_text(prompts_dir / _PREFIX_FILE)
        schemas = _load_schemas(contracts_dir)
        registry: Registry[Schema] = Registry().with_resources(
            (str(schema["$id"]), Resource.from_contents(schema, default_specification=DRAFT202012))
            for schema in schemas.values()
            if "$id" in schema
        )
        tasks: dict[str, PromptTask] = {}
        for task_dir in sorted(p for p in prompts_dir.iterdir() if p.is_dir()):
            tasks[task_dir.name] = _load_task(task_dir, prefix, schemas, registry)
        return cls(tasks)

    def get(self, task_type: str) -> PromptTask | None:
        """taskType의 프롬프트. 모르는 taskType이면 None(R-3)."""
        return self._tasks.get(task_type)

    def task_types(self) -> list[str]:
        return sorted(self._tasks)


def _load_task(
    task_dir: Path,
    prefix: str,
    schemas: dict[Path, JsonObject],
    registry: Registry[Schema],
) -> PromptTask:
    instruction = _read_text(task_dir / _PROMPT_FILE)
    input_path = (task_dir / _INPUT_FILE).resolve()
    output_path = (task_dir / _OUTPUT_FILE).resolve()
    for path in (input_path, output_path):
        if path not in schemas:
            raise PromptRegistryError(f"missing schema: {path}")
    referenced = sorted(
        _referenced_files(input_path, schemas) | _referenced_files(output_path, schemas)
    )
    digest = hashlib.sha256()
    for part in (
        prefix,
        instruction,
        _canonical(schemas[input_path]),
        _canonical(schemas[output_path]),
        *(_canonical(schemas[p]) for p in referenced),
    ):
        # 구분자를 넣어 경계가 다른 두 내용이 같은 해시가 되지 않게 한다.
        digest.update(part.encode("utf-8"))
        digest.update(b"\x00")
    return PromptTask(
        task_type=task_dir.name,
        version=f"{task_dir.name}@{digest.hexdigest()[:_VERSION_DIGITS]}",
        fixed_prefix=prefix,
        instruction=instruction,
        input_validator=Draft202012Validator(schemas[input_path], registry=registry),
        output_validator=Draft202012Validator(schemas[output_path], registry=registry),
    )


def _load_schemas(contracts_dir: Path) -> dict[Path, JsonObject]:
    schemas: dict[Path, JsonObject] = {}
    for folder in ("prompts", "taxonomy"):
        for path in sorted((contracts_dir / folder).rglob("*.schema.json")):
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
            except (json.JSONDecodeError, SchemaError) as e:
                raise PromptRegistryError(f"invalid schema {path}: {e}") from e
            if not isinstance(schema, dict):
                raise PromptRegistryError(f"schema is not an object: {path}")
            schemas[path.resolve()] = schema
    return schemas


def _referenced_files(path: Path, schemas: dict[Path, JsonObject]) -> set[Path]:
    """스키마가 `$ref`로 참조하는 다른 파일(분류 체계 등)을 재귀로 모은다."""
    found: set[Path] = set()
    pending = [path]
    while pending:
        current = pending.pop()
        for ref in _refs(schemas[current]):
            target = ref.split("#", 1)[0]
            if not target:
                continue
            resolved = (current.parent / target).resolve()
            if resolved not in schemas:
                raise PromptRegistryError(f"{current} refers to missing schema {target}")
            if resolved not in found:
                found.add(resolved)
                pending.append(resolved)
    return found


def _refs(node: object) -> list[str]:
    if isinstance(node, dict):
        refs = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        return refs + [r for value in node.values() for r in _refs(value)]
    if isinstance(node, list):
        return [r for item in node for r in _refs(item)]
    return []


def _canonical(schema: JsonObject) -> str:
    # 들여쓰기·키 순서만 바뀐 JSON은 같은 버전이 되게 한다.
    return json.dumps(schema, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise PromptRegistryError(f"missing prompt file: {path}") from e


def _errors(validator: Draft202012Validator, instance: object) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in error.absolute_path) or '<root>'}: {error.message}"
        for error in validator.iter_errors(instance)
    ]
