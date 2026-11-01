<!-- 규칙 원본은 AGENTS.md다(Codex·Antigravity와 공유). 여기에는 Claude Code 전용 내용만 둔다. -->
@AGENTS.md

## Claude Code 전용
- 루트 구조(`project/repos/ai`)로 클론했다면 상위의 `../../CLAUDE.md`가 자동으로 함께 로드된다. 상위 파일을 따로 읽을 필요가 없다.
- `.claude/rules/`의 규칙은 `paths`에 맞는 파일을 다룰 때 자동으로 로드된다.
- 스킬은 `/sdd-plan`, `/sdd-implement`로 호출한다.
- 개인적으로 설치한 플러그인 스킬(TDD, 코드 리뷰 등)과 이 프로젝트의 규칙(AGENTS.md, `.claude/rules/`, `.claude/skills/`)이 다르면 프로젝트 규칙이 우선한다.
- `.claude/rules/`나 `.claude/skills/`를 수정했다면 `./scripts/sync-agent-config.sh`를 실행해서 `.agents/`에 반영한다.
