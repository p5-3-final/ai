# AGENTS.md — ai (Python uv workspace: ai-gateway, Data Plane 워커)

<!-- 이 레포 AI 코딩 도구 공통 규칙의 원본이다. CLAUDE.md는 이 파일을 import한다. 여기에는 ai 고유 규칙만 둔다. -->

## 상위 규칙 (필수)
- 공통 규칙(SDD, TDD, 주석, lint·검사 예외, Git, 언어, 보안)은 docs 루트의 `../../AGENTS.md`에 있다.
  - Claude Code와 Antigravity(루트 `project/` 폴더를 연 경우)는 이 파일을 자동으로 불러온다.
  - **Codex처럼 상위 파일을 자동으로 읽지 않는 도구는 작업을 시작하기 전에 `../../AGENTS.md`를 직접 읽는다.**
- 경로별 세부 규칙은 아래 표와 같다. 해당 파일을 다룰 때 자동으로 불러오지 않는 도구는 직접 읽는다. `.agents/`는 `scripts/sync-agent-config.sh`가 만드는 생성본이므로 수정하지 않는다.

| 작업 대상 | 규칙 파일 |
| --- | --- |
| `services/**/*.py`, `libs/**/*.py`, `evals/**/*.py` | `.claude/rules/python-style.md` |
| `**/pyproject.toml` (검사 도구 설정) | `.claude/rules/lint.md` |
| `.github/workflows/**`, `Dockerfile`, `*.sh`, `.editorconfig`, `lefthook.yml` | `.claude/rules/ci-common.md` |

## 스택
Python 3.12 · uv workspace · FastAPI(ai-gateway) · Kafka 소비 워커 · Pydantic v2 · pydantic-settings · httpx · ruff(lint+format) · mypy(strict) · pytest · pytest-asyncio

## 구조 (uv workspace)
배포 단위 9개를 workspace 멤버로 나눈다. 결정 배경은 docs `docs/adr/0001-ai-uv-workspace.md`에 있다.
```
ai/
├── pyproject.toml / uv.lock      # workspace 루트. 멤버 목록과 공통 개발 도구(ruff, mypy, pytest). lock은 하나
├── libs/
│   ├── common/                   # config(Settings), logging, errors, health
│   ├── messaging/                # Kafka 소비·발행, retry 토픽, 연속 offset 커밋, eventId 멱등 처리
│   ├── contracts/                # 이벤트 Pydantic 모델 (docs/contracts/events의 JSON Schema가 원본)
│   └── ai-client/                # ai-gateway 호출 클라이언트 (/v1/analyze, /v1/embed)
├── services/
│   ├── ai-gateway/               # FastAPI. 모델 라우팅, 캐시·예산·토큰 버킷, Adapter(Mock·Local·OpenAI)
│   │   └── prompts/              # taskType별 프롬프트·출력 스키마 (Prompt Registry, 버전 관리)
│   ├── normalize/                # 영상 검증, 정규화 사본 (FFmpeg)
│   ├── chunk-processor/          # 청크 디코딩, 구간 샘플링, 객체 추적(YOLO), 이벤트 후보, 블러
│   ├── track-stitcher/           # 청크 경계 트랙 연결
│   ├── track-analyzer/           # 트랙 속성 분석
│   ├── event-analyzer/           # 이벤트 후보 병합, 판정
│   ├── vlm-worker/               # 구간 설명, 구간 VLM 이벤트 후보
│   ├── embedding-worker/         # 설명 텍스트 묶음 임베딩
│   └── aggregator/               # 청크 요약, 전체 요약, 이벤트 타임라인
├── evals/                        # Golden Set 평가 스크립트, 결과 기록
├── notebooks/                    # 실험용 (services·libs에서 import 금지, 배포 제외)
└── specs/SPEC-NNN-*/             # plan.md, tasks.md
```
- 멤버마다 `pyproject.toml`, `src/<패키지>/`, `tests/`를 둔다. 폴더 이름은 kebab-case, 패키지 이름은 snake_case다(`vlm-worker` → `vlm_worker`).
- 서비스는 다른 서비스를 import하지 않는다. 공통 코드는 `libs/`로 옮긴다. 서비스마다 Dockerfile을 두고 독립 이미지로 배포한다.
- 외부 AI API는 ai-gateway만 호출한다. 워커는 `libs/ai-client`로 ai-gateway를 부른다.

## 명령어
```bash
uv sync --all-packages                          # 의존성 설치 (lock 기준, 전 멤버)
uv run --package ai-gateway fastapi dev services/ai-gateway/src/ai_gateway/main.py  # ai-gateway 실행 (:8000)
uv run --package vlm-worker python -m vlm_worker   # 워커 실행
uv run pytest                                   # 전체 테스트
uv run pytest services/vlm-worker -k segment    # 일부만
uv run ruff check --fix . && uv run ruff format .   # 고치기
uv run ruff check . && uv run ruff format --check . # 검사만
uv run mypy services libs
uv run python -m evals.run --suite <name>       # 평가
```
작업을 끝내기 전에 `ruff check`, `ruff format --check`, `mypy services libs`, `pytest`를 실행한다.

## lint
도구: ruff(format, check), mypy(strict). 공통 도구(workflow, Dockerfile, lefthook)는 `.claude/rules/ci-common.md`에 있다.

규칙 세트와 설정 위치는 `.claude/rules/lint.md`에 있다. **검사 도구의 설정 파일을 만들거나 고칠 때는 이 파일을 먼저 읽는다.**

## 핵심 규칙
- **의존성**: `uv add <pkg>`로만 추가하고 `uv.lock`을 커밋한다. `pip install`과 `requirements.txt`는 쓰지 않는다. 새 패키지는 사용자에게 확인받는다.
- **타입**: 모든 함수에 타입 힌트를 단다. `mypy --strict`를 통과해야 한다. `Any`는 쓰지 않는다. 불가피하면 같은 줄에 사유와 이슈 번호를 주석으로 남긴다(`# 사유 (#87)`). `# type: ignore`는 루트 AGENTS.md의 "검사 예외 (무시 주석)" 형식을 따른다.
- **계약**: ai-gateway의 요청·응답은 Pydantic 모델로 정의하고 `../../docs/contracts/openapi/ai-gateway.yaml`과 일치시킨다. 이벤트는 `libs/contracts`의 모델을 쓰고 `../../docs/contracts/events/`의 JSON Schema와 계약 테스트로 맞춘다. 출력 스키마의 enum은 `../../docs/contracts/taxonomy/`를 따른다.
- **오류 응답**: 오류는 RFC 9457 ProblemDetail 형식(`code` 필드 포함)으로 반환한다. 공통 예외 핸들러는 `libs/common`에 둔다.
- **비동기**: I/O는 `async`로 처리한다. CPU를 많이 쓰는 추론은 이벤트 루프를 막지 않도록 `run_in_threadpool` 또는 별도 워커로 분리한다.
- **모델 로딩**: 모델은 요청마다 로드하지 않고 `lifespan`에서 한 번 로드해서 `app.state`에 보관한다. 모델 가중치와 데이터셋은 git에 커밋하지 않고 S3에 둔다.
- **설정**: `pydantic-settings`의 `Settings` 클래스로 환경변수를 읽는다. `os.environ`을 직접 읽지 않는다. 비밀값은 `SecretStr`로 감싼다.
- **외부 LLM·API 호출**: 타임아웃, 재시도(지수 백오프), 토큰·비용 로깅을 반드시 둔다. API 키는 로그에 남기지 않는다.
- **프롬프트**: 프롬프트는 코드에 긴 문자열로 하드코딩하지 않는다. `services/ai-gateway/prompts/`에 버전을 붙여 관리한다(`segment_describe_v2.md`).
- **평가**: 모델이나 프롬프트를 바꾸면 `evals`를 실행하고, 변경 전후 지표를 PR 본문에 첨부한다.
- **로그**: `logging` 모듈로 JSON 구조화 로그를 남긴다. `print`는 금지한다. 사용자 입력 원문과 개인정보는 로그에 남기지 않는다.
- **health**: ai-gateway는 `/health`(liveness)와 `/ready`를 제공한다. 워커는 `libs/common`의 health 엔드포인트로 같은 경로를 연다.

## 테스트
- 단위 테스트에서는 외부 LLM·API를 실제로 호출하지 않는다. `respx`나 의존성 오버라이드(`app.dependency_overrides`)로 대체한다. 실제 호출은 `evals`에서만 한다.
- API 테스트는 `httpx.AsyncClient` + `ASGITransport`로 작성한다.
- 테스트 이름이나 docstring에 요구사항 ID를 포함한다: `test_spec012_r1_returns_summary` / `"""[SPEC-012 R-1] ..."""`
- 모델 출력처럼 결과가 비결정적인 부분은 정확한 문자열을 비교하지 말고 스키마와 불변 조건을 검증한다.

## CI
- PR일 때: `ruff check`, `ruff format --check`, `mypy services libs`, `pytest`를 실행한다. 공통 검사(hadolint, actionlint, zizmor, PR 제목)는 `.claude/rules/ci-common.md`를 따른다. main 머지일 때: 바뀐 서비스(또는 그 서비스가 쓰는 `libs/`)만 이미지 빌드 → ECR push(OIDC) → gitops 레포 `envs/dev` 태그 갱신
- Dockerfile은 멀티스테이지로 만든다. uv로 의존성을 설치하고, non-root 사용자로 실행한다. 모델 가중치는 이미지에 넣지 않고 기동할 때 다운로드한다.
