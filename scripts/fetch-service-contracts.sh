#!/usr/bin/env bash
# 서비스 간 계약을 service-contracts.lock의 버전으로 받는다(docs ADR-0013).
#   scripts/fetch-service-contracts.sh               # 잠금 버전을 받아 폴더 경로를 출력한다
#   scripts/fetch-service-contracts.sh --update 1.2.0 # 잠금 파일을 새 버전으로 바꾼다
# 받은 계약은 .service-contracts/service-contracts/에 풀린다. CI는 이 경로를 CONTRACTS_DIR로 넘긴다.
# 로컬은 작업 공간의 docs 계약을 바로 읽으므로 보통은 실행하지 않는다.
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
lock="${root}/service-contracts.lock"
cache="${root}/.service-contracts"
release_url="https://github.com/p5-3-final/docs/releases/download"

sha256_of() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

archive_url() {
  printf '%s/service-contracts-v%s/service-contracts-%s.tar.gz' "${release_url}" "$1" "$1"
}

if [[ "${1:-}" == "--update" ]]; then
  version="${2:?새 버전을 넘긴다. 예: --update 1.2.0}"
  tmp="$(mktemp -d)"
  trap 'rm -rf "${tmp}"' EXIT
  curl -fsSL -o "${tmp}/archive.tar.gz" "$(archive_url "${version}")"
  # 릴리스에 함께 올린 SHA-256과 맞을 때만 잠금 파일에 적는다.
  expected="$(curl -fsSL "$(archive_url "${version}").sha256" | cut -d' ' -f1)"
  actual="$(sha256_of "${tmp}/archive.tar.gz")"
  if [[ "${expected}" != "${actual}" ]]; then
    echo "체크섬이 릴리스와 다르다: ${actual} != ${expected}" >&2
    exit 1
  fi
  cat > "${lock}" <<LOCK
# 서비스 간 계약 버전 고정(docs ADR-0013). CI가 이 버전을 받아 CONTRACTS_DIR로 넘긴다.
# 올리기: scripts/fetch-service-contracts.sh --update <버전>
version=${version}
sha256=${actual}
LOCK
  echo "service-contracts.lock → ${version}"
  exit 0
fi

version="$(sed -n 's/^version=//p' "${lock}")"
sha256="$(sed -n 's/^sha256=//p' "${lock}")"
target="${cache}/service-contracts"
stamp="${cache}/.locked"

# 같은 잠금으로 이미 받았으면 다시 받지 않는다.
if [[ -f "${stamp}" ]] && [[ "$(cat "${stamp}")" == "${version} ${sha256}" ]] && [[ -d "${target}" ]]; then
  echo "${target}"
  exit 0
fi

rm -rf "${cache}"
mkdir -p "${cache}"
curl -fsSL -o "${cache}/archive.tar.gz" "$(archive_url "${version}")"
actual="$(sha256_of "${cache}/archive.tar.gz")"
if [[ "${actual}" != "${sha256}" ]]; then
  echo "체크섬이 잠금 파일과 다르다: ${actual} != ${sha256}" >&2
  exit 1
fi
tar -xzf "${cache}/archive.tar.gz" -C "${cache}"
rm "${cache}/archive.tar.gz"
printf '%s %s\n' "${version}" "${sha256}" > "${stamp}"
echo "${target}"
