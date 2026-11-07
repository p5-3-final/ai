#!/usr/bin/env bash
# 내부 호출 클라이언트의 요청·응답 모델을 계약에서 생성한다(SPEC-021 R-6·R-7).
#   uv run scripts/generate-clients.sh                       # 모든 클라이언트를 다시 만든다
#   uv run scripts/generate-clients.sh ai-client             # 하나만
#   uv run scripts/generate-clients.sh ai-client <출력 파일> <계약 폴더>  # 비교 테스트가 임시 경로에 만든다
# 계약 폴더는 셋째 인자, 없으면 CONTRACTS_DIR(CI는 잠금 버전), 그것도 없으면 작업 공간 docs 레포의
# service-contracts/다.
# 생성 옵션은 각 lib의 pyproject.toml [tool.datamodel-codegen]에 있다. datamodel-codegen·ruff는 PATH에서 찾는다.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
default_contracts="${CONTRACTS_DIR:-${root}/../../service-contracts}"
header="# 생성 코드. scripts/generate-clients.sh로 만든다. 손으로 고치지 않는다."

# lib 폴더 이름 → "<계약 파일> <패키지>". 새 클라이언트는 여기에 한 줄 더한다.
target_of() {
  case "$1" in
    ai-client) echo "openapi/ai-gateway.yaml vlm_ai_client" ;;
    analysis-client) echo "openapi/analysis-service.yaml vlm_analysis_client" ;;
    stream-client) echo "openapi/stream-service.yaml vlm_stream_client" ;;
    *)
      echo "모르는 클라이언트: $1" >&2
      return 1
      ;;
  esac
}
all_targets=(ai-client analysis-client stream-client)

generate() {
  local lib="$1" out="${2:-}" contracts="${3:-${default_contracts}}" contract package
  read -r contract package <<<"$(target_of "${lib}")"
  out="${out:-${root}/libs/${lib}/src/${package}/models.py}"
  # 생성기는 실행 폴더의 pyproject.toml에서 옵션을 읽는다.
  (
    cd "${root}/libs/${lib}"
    datamodel-codegen \
      --input "${contracts}/${contract}" \
      --output "${out}" \
      --custom-file-header "${header}"
  )
  # 로컬·CI·임시 경로 어디서 만들어도 같은 결과가 되도록 레포 설정을 직접 넘긴다.
  # 생성기가 만드는 TypeAliasType 별칭(UP040)은 `type` 문으로 바꿔야 검사를 통과하는데, ruff가 이 수정을
  # unsafe로 분류해 따로 허용한다(analysis-service.yaml, SPEC-027).
  ruff check --config "${root}/pyproject.toml" --fix-only --unsafe-fixes --quiet "${out}"
  ruff format --config "${root}/pyproject.toml" --quiet "${out}"
}

if [[ $# -eq 0 ]]; then
  for lib in "${all_targets[@]}"; do
    generate "${lib}"
  done
else
  generate "$@"
fi
