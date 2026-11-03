#!/usr/bin/env bash
# ffmpeg가 필요한 서비스 테스트를 운영과 같은 이미지(test 단계)에서 실행한다.
#   scripts/test-in-docker.sh normalize                 # services/normalize 전체
#   scripts/test-in-docker.sh normalize -k decision     # pytest 인자를 그대로 넘긴다
# 소스는 마운트하므로 코드만 바꾸면 이미지를 다시 빌드하지 않는다(의존성이 바뀌면 다시 빌드된다).
set -euo pipefail

service="${1:?서비스 이름을 넘긴다. 예: normalize}"
shift
root="$(cd "$(dirname "$0")/.." && pwd)"
contracts="${CONTRACTS_DIR:-${root}/../../docs/contracts}"
image="vlm/${service}-test"

docker build --quiet -f "${root}/services/${service}/Dockerfile" --target test -t "${image}" "${root}" >/dev/null

# testcontainers가 띄운 형제 컨테이너에는 호스트를 거쳐 붙는다.
docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --add-host=host.docker.internal:host-gateway \
  -e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
  -e CONTRACTS_DIR=/contracts \
  -v "${contracts}:/contracts:ro" \
  -v "${root}:/app" \
  -v /app/.venv \
  "${image}" \
  uv run --frozen pytest "services/${service}" "$@"
