"""SPEC-011 NFR-4: open_clip·torch는 인코더 구현 모듈에서만 import한다.

frame-encoder처럼 인터페이스만 쓰는 쪽이 `base`를 import해도 torch를 끌고 오지 않게 한다.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "vlm_encoders"
ALLOWED = {
    "open_clip": {"open_clip.py"},
    "torch": {"open_clip.py"},
    "PIL": {"open_clip.py"},
    "safetensors": {"open_clip.py"},
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
def test_spec011_nfr4_encoder_libraries_only_in_implementation(module: str) -> None:
    users = {
        str(path.relative_to(SRC)) for path in SRC.rglob("*.py") if module in _imported_roots(path)
    }

    assert users <= ALLOWED[module]
