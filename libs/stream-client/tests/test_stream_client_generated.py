"""커밋한 생성 모델이 잠금 계약에서 다시 생성한 결과와 같은지 확인한다.

SPEC-027 R-33, SPEC-021 R-7.
"""

import shutil
import subprocess
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "generate-clients.sh"
_COMMITTED = _ROOT / "libs" / "stream-client" / "src" / "vlm_stream_client" / "models.py"


def _default_contracts_dir() -> Path:
    # 로컬은 작업 공간(project/repos/ai) 두 단계 위 docs 레포, CI는 CONTRACTS_DIR(docs ADR-0013)
    parents = Path(__file__).resolve().parents
    return parents[5] / "service-contracts" if len(parents) > 5 else Path("/contracts")


class _ContractSettings(BaseSettings):
    contracts_dir: Path = Field(default_factory=_default_contracts_dir)


def test_spec027_r33_committed_models_match_regenerated(tmp_path: Path) -> None:
    """[SPEC-027 R-33] 계약을 올리고 다시 생성하지 않으면 이 테스트가 실패한다."""
    contracts = _ContractSettings().contracts_dir
    assert (contracts / "openapi").is_dir(), f"계약이 없다: {contracts} (CONTRACTS_DIR 확인)"
    out = tmp_path / "models.py"
    bash = shutil.which("bash")
    assert bash is not None

    result = subprocess.run(  # noqa: S603  # 레포 스크립트와 고정 인자만 쓰며 셸을 쓰지 않는다 (#44)
        [bash, str(_SCRIPT), "stream-client", str(out), str(contracts)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert out.read_text(encoding="utf-8") == _COMMITTED.read_text(encoding="utf-8"), (
        "생성 모델이 계약과 다르다: uv run scripts/generate-clients.sh stream-client"
    )
