---
trigger: glob
globs: ".github/workflows/**, **/Dockerfile, **/*.sh, .editorconfig, lefthook.yml"
---
<!-- 자동 생성 파일: .claude/rules/ 원본을 수정하고 scripts/sync-agent-config.sh 를 실행하세요 -->

# 공통 검사 도구 (workflow · Dockerfile · 셸 · 에디터 · git hook)

모든 레포가 같은 기준으로 쓰는 검사다. 레포 고유의 lint는 각 레포의 `.claude/rules/lint.md`에 있다.

| 대상 | 도구 | 검사 내용 |
| --- | --- | --- |
| 모든 파일 | `.editorconfig` | UTF-8, LF, 파일 끝 개행, 줄 끝 공백 제거, 들여쓰기 |
| `.github/workflows/**` | actionlint, zizmor | workflow 문법, 보안(과도한 권한, 스크립트 주입, SHA로 고정하지 않은 action) |
| `Dockerfile` | hadolint | 베이스 이미지 태그 고정, non-root 실행, 레이어 구성 |
| 셸 스크립트 | shellcheck | 따옴표 누락, 실패 처리 |
| PR 제목 | `amannn/action-semantic-pull-request` | Conventional Commits 형식. squash merge라서 PR 제목이 그대로 커밋 메시지가 된다 |

## git hook (lefthook)
- `lefthook.yml`의 `pre-commit`에서 바뀐 파일(`{staged_files}`)에만 포맷과 lint를 실행한다. 전체 테스트는 넣지 않는다(커밋이 느려진다).
- hook은 빠른 피드백용이고, 관문은 CI다. CI는 같은 검사를 전체 대상으로 다시 실행한다.
- `--no-verify`로 hook을 건너뛰지 않는다.

## PR 제목 검사
- `amannn/action-semantic-pull-request`의 `types`는 루트 AGENTS.md "Git 규칙"의 type 목록과 같게 지정한다. action 기본값(`style`, `revert` 포함)을 그대로 쓰지 않는다.
- scope는 서비스, 모듈, 환경 이름이다(`user-service`, `api-gateway`, `envs-dev` 등).

## workflow
- workflow를 고쳤다면 `actionlint`와 `zizmor .github/workflows`를 실행하고 결과를 보고한다. CI에도 같은 검사 job을 둔다.
- 기본 권한은 `permissions: contents: read`이고, 필요한 job에만 권한을 더한다.
- 서드파티 action은 커밋 SHA로 고정하고, 옆에 버전 주석을 단다(`# v4.2.0`).
