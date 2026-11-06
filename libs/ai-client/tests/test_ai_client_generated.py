"""커밋한 생성 모델이 잠금 계약에서 다시 생성한 결과와 같은지 확인한다(SPEC-021 R-7)."""

import shutil
import subprocess
from pathlib import Path

from ac_support import contracts_dir

_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _ROOT / "scripts" / "generate-clients.sh"
_COMMITTED = _ROOT / "libs" / "ai-client" / "src" / "vlm_ai_client" / "models.py"


def test_spec021_r7_committed_models_match_regenerated(tmp_path: Path) -> None:
    """[SPEC-021 R-7] 계약을 올리고 다시 생성하지 않으면 이 테스트가 실패한다."""
    out = tmp_path / "models.py"
    bash = shutil.which("bash")
    assert bash is not None

    # `uv run pytest`가 PATH에 가상환경을 넣으므로 스크립트가 같은 datamodel-codegen·ruff를 쓴다.
    result = subprocess.run(  # noqa: S603  # 레포 스크립트와 고정 인자만 쓰며 셸을 쓰지 않는다 (#44)
        [bash, str(_SCRIPT), "ai-client", str(out), str(contracts_dir())],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert _COMMITTED.is_file(), "생성 모델이 없다: scripts/generate-clients.sh ai-client"
    assert out.read_text(encoding="utf-8") == _COMMITTED.read_text(encoding="utf-8"), (
        "생성 모델이 계약과 다르다: uv run scripts/generate-clients.sh ai-client 로 다시 만든다"
    )
