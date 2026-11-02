#!/usr/bin/env bash
# .claude/(rules, skills) 원본을 Codex·Antigravity가 읽는 .agents/(rules, skills)로 변환·복사한다.
#   rules : Claude `paths:` frontmatter → Antigravity `trigger: glob` + `globs:` (paths 없으면 always_on)
#   skills: SKILL.md 형식이 같으므로 그대로 복사
# 사용법: ./scripts/sync-agent-config.sh [대상 디렉터리] [--check]
#   대상 디렉터리 기본값은 이 스크립트가 있는 레포 루트. --check는 생성본이 최신인지 검사만 한다(CI용).
set -euo pipefail

target="$(cd "$(dirname "$0")/.." && pwd)"
check=0
for arg in "$@"; do
  case "$arg" in
    --check) check=1 ;;
    *) target="$(cd "$arg" && pwd)" ;;
  esac
done

src="$target/.claude"
out="$target/.agents"
[[ -d "$src" ]] || { echo "error: $src 가 없습니다" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/rules" "$tmp/skills"

convert_rule() {
  awk '
    function expand(g,    pre, body, post, n, parts, i, res) {
      # 중괄호 그룹 하나를 펼친다: src/*.{ts,tsx} → src/*.ts, src/*.tsx (Antigravity globs는 쉼표로 구분)
      if (!match(g, /\{[^}]*\}/)) return g
      pre = substr(g, 1, RSTART - 1)
      body = substr(g, RSTART + 1, RLENGTH - 2)
      post = substr(g, RSTART + RLENGTH)
      n = split(body, parts, ",")
      res = ""
      for (i = 1; i <= n; i++) res = res (i > 1 ? ", " : "") pre parts[i] post
      return res
    }
    function header() {
      print "---"
      if (globs != "") { print "trigger: glob"; print "globs: \"" globs "\"" }
      else print "trigger: always_on"
      print "---"
      print "<!-- 자동 생성 파일: .claude/rules/ 원본을 수정하고 scripts/sync-agent-config.sh 를 실행하세요 -->"
      print ""
    }
    BEGIN { state = "start" }
    state == "start" {
      if ($0 == "---") { state = "fm"; next }
      header(); state = "body"
    }
    state == "fm" {
      if ($0 == "---") { header(); state = "skipblank"; next }
      if ($0 ~ /^paths:/) { inpaths = 1; next }
      if (inpaths && $0 ~ /^[[:space:]]*-/) {
        g = $0
        sub(/^[[:space:]]*-[[:space:]]*/, "", g)
        gsub(/["\047]/, "", g)
        g = expand(g)
        globs = (globs == "" ? g : globs ", " g)
        next
      }
      inpaths = 0
      next
    }
    state == "skipblank" { state = "body"; if ($0 == "") next }
    { print }
  ' "$1"
}

if [[ -d "$src/rules" ]]; then
  # Antigravity는 .agents/rules/ 바로 아래 파일만 읽으므로 하위 폴더는 지원하지 않는다.
  for f in "$src"/rules/*.md; do
    [[ -e "$f" ]] || continue
    convert_rule "$f" > "$tmp/rules/$(basename "$f")"
  done
fi

if [[ -d "$src/skills" ]]; then
  cp -R "$src/skills/." "$tmp/skills/"
fi

if [[ "$check" == 1 ]]; then
  if diff -r "$tmp/rules" "$out/rules" >/dev/null 2>&1 && diff -r "$tmp/skills" "$out/skills" >/dev/null 2>&1; then
    echo "ok: .agents/ 가 최신입니다"
    exit 0
  fi
  echo "error: .agents/ 가 .claude/ 와 다릅니다. ./scripts/sync-agent-config.sh 를 실행하세요." >&2
  diff -r "$tmp" "$out" >&2 || true
  exit 1
fi

mkdir -p "$out"
rm -rf "$out/rules" "$out/skills"
cp -R "$tmp/rules" "$out/rules"
cp -R "$tmp/skills" "$out/skills"

# 대상 폴더가 비어 있으면 glob이 패턴 문자열 그대로 남아 1개로 세이므로 nullglob을 켠다.
shopt -s nullglob
rules=("$out"/rules/*)
skills=("$out"/skills/*)
echo "sync 완료: $out (rules ${#rules[@]}개, skills ${#skills[@]}개)"
