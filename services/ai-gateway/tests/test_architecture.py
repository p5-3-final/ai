"""SPEC-009 NFR-2, SPEC-011 NFR-4: 공급자 SDK·인코더 라이브러리는 구현 한 곳에서만 부른다."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "ai_gateway"
# 모듈 → import해도 되는 파일(SRC 기준)
ALLOWED = {
    "openai": {"adapters/openai.py"},
    "httpx2": {"adapters/openai.py"},
    # 저장소·Kafka 클라이언트는 libs 구현을 거쳐서만 쓴다.
    "boto3": set(),
    "botocore": set(),
    "confluent_kafka": set(),
    # 인코더 라이브러리는 libs/encoders 구현을 거쳐서만 쓴다(SPEC-011 NFR-4).
    "open_clip": set(),
    "torch": set(),
    "safetensors": set(),
    "PIL": set(),
}


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_spec009_nfr2_provider_sdk_only_in_adapter(module: str) -> None:
    users = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if module in _imported_roots(path)
    }

    assert users <= ALLOWED[module]
