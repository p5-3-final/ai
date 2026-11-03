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

쓰는 모델은 프로필 값이다. 현재 기본값과 근거는 docs `docs/adr/0008-industrial-safety-v3.md`에 있다.

## 구조 (uv workspace)
배포 단위 10개를 workspace 멤버로 나눈다. 결정 배경은 docs `docs/adr/0001-ai-uv-workspace.md`, 서비스별 책임과 이벤트는 `docs/architecture/service-catalog.md`에 있다.
```
ai/
├── pyproject.toml / uv.lock      # workspace 루트. 멤버 목록과 공통 개발 도구(ruff, mypy, pytest). lock은 하나
├── libs/
│   ├── common/                   # config(Settings), logging, errors, health, 프로필 로딩·검증
│   ├── messaging/                # Kafka 소비·발행, retry·DLQ 토픽, 처리 결과가 정해진 뒤 수동 커밋
│   ├── contracts/                # 이벤트 Pydantic 모델 (docs/contracts/events의 JSON Schema가 원본)
│   ├── storage/                  # 객체 저장소 인터페이스(ObjectStorage)와 S3 API 구현, 테스트용 메모리 구현
│   ├── testing/                  # 테스트 전용: testcontainers로 Kafka·SeaweedFS 띄우기 (운영 이미지에 넣지 않음)
│   └── ai-client/                # ai-gateway 호출 클라이언트 (/v1/analyze, /v1/embed, /v1/parse)
├── services/
│   ├── ai-gateway/               # FastAPI. 외부 AI 호출 관문, 질의 해석·질의 벡터
│   │   └── prompts/              # taskType별 프롬프트·출력 스키마 (Prompt Registry, 버전 관리)
│   ├── normalize/                # 영상 검증, 정규화 사본
│   ├── chunk-processor/          # 청크 디코딩, 구간 프레임 추출, 탐지·추적, 이벤트 후보
│   ├── track-stitcher/           # 청크 경계 트랙 연결, Re-ID
│   ├── frame-encoder/            # 구간·크롭 이미지 벡터
│   ├── grounding-worker/         # 검색 결과 bbox 주석 클립
│   ├── event-analyzer/           # 이벤트 후보 VLM 판정
│   ├── track-analyzer/           # 트랙 속성 분석, 객체 질문
│   ├── embedding-worker/         # 텍스트 임베딩
│   └── aggregator/               # 영상 단위 병합, 위험 상태 전이, 요약
├── evals/                        # Golden Set 평가 스크립트, 결과 기록
├── notebooks/                    # 실험용 (services·libs에서 import 금지, 배포 제외)
└── specs/SPEC-NNN-*/             # plan.md, tasks.md
```
- 멤버마다 `pyproject.toml`, `src/<패키지>/`, `tests/`를 둔다. 폴더 이름은 kebab-case, 패키지 이름은 snake_case다(`frame-encoder` → `frame_encoder`). `libs/`는 이름이 흔해 겹치지 않도록 `vlm_` 접두사를 붙인다(`libs/common` → 배포 이름 `vlm-common`, 패키지 `vlm_common`).
- 새 멤버를 만들면 루트 `pyproject.toml`의 `[tool.uv.workspace] members`에 추가한다. 다른 멤버를 의존하면 `[tool.uv.sources]`에 `{ workspace = true }`로 적는다.
- 서비스는 다른 서비스를 import하지 않는다. 공통 코드는 `libs/`로 옮긴다. 서비스마다 Dockerfile을 두고 독립 이미지로 배포한다.
- 외부 AI API는 ai-gateway만 호출한다. 워커는 `libs/ai-client`로 ai-gateway를 부른다.
- 자체 실행 모델(인코더·탐지기)의 공통 인터페이스와 구현은 `libs/`에 두어 여러 서비스가 같은 구현을 쓴다(예: frame-encoder와 ai-gateway의 인코더).

## 명령어
```bash
uv sync --all-packages                          # 의존성 설치 (lock 기준, 전 멤버)
uv run --package ai-gateway fastapi dev services/ai-gateway/src/ai_gateway/main.py  # ai-gateway 실행 (:8000)
uv run --package event-analyzer python -m event_analyzer   # 워커 실행
uv run pytest                                   # 전체 테스트
uv run pytest services/event-analyzer -k judge  # 일부만
uv run pytest -m "not integration"              # Docker(testcontainers)가 필요한 테스트 제외
uv run pytest -m "not ffmpeg"                   # ffmpeg가 필요한 테스트 제외(로컬 빠른 반복)
scripts/test-in-docker.sh normalize             # 서비스 이미지 안에서 그 서비스 테스트 전체(ffmpeg 포함)
uv run ruff check --fix . && uv run ruff format .   # 고치기
uv run ruff check . && uv run ruff format --check . # 검사만
uv run mypy                                     # 대상은 pyproject.toml [tool.mypy] files
uv run python -m evals.run --suite <name>       # 평가
```
작업을 끝내기 전에 `ruff check`, `ruff format --check`, `mypy`, `pytest -m "not ffmpeg"`, 바꾼 서비스의 `scripts/test-in-docker.sh <서비스>`를 실행한다. ffmpeg처럼 시스템 패키지가 필요한 테스트는 `ffmpeg` 마커를 달고 운영과 같은 이미지에서 돌린다(로컬에 설치하지 않는다).

## lint
도구: ruff(format, check), mypy(strict). 공통 도구(workflow, Dockerfile, lefthook)는 `.claude/rules/ci-common.md`에 있다.

규칙 세트와 설정 위치는 `.claude/rules/lint.md`에 있다. **검사 도구의 설정 파일을 만들거나 고칠 때는 이 파일을 먼저 읽는다.**

## 핵심 규칙
- **의존성**: `uv add <pkg>`로만 추가하고 `uv.lock`을 커밋한다. `pip install`과 `requirements.txt`는 쓰지 않는다. 새 패키지는 사용자에게 확인받는다.
- **타입**: 모든 함수에 타입 힌트를 단다. `mypy --strict`를 통과해야 한다. `Any`는 쓰지 않는다. 불가피하면 같은 줄에 사유와 이슈 번호를 주석으로 남긴다(`# 사유 (#87)`). `# type: ignore`는 루트 AGENTS.md의 "검사 예외 (무시 주석)" 형식을 따른다.
- **계약**: ai-gateway의 요청·응답은 Pydantic 모델로 정의하고 `../../docs/contracts/openapi/ai-gateway.yaml`과 일치시킨다. 이벤트는 `libs/contracts`의 모델을 쓰고 `../../docs/contracts/events/`의 JSON Schema와 계약 테스트로 맞춘다. 출력 스키마의 enum은 `../../docs/contracts/taxonomy/`를 따른다.
- **오류 응답**: 오류는 RFC 9457 ProblemDetail 형식(`code` 필드 포함)으로 반환한다. 공통 예외 핸들러는 `libs/common`에 둔다.
- **비동기**: I/O는 `async`로 처리한다. CPU를 많이 쓰는 추론은 이벤트 루프를 막지 않도록 `run_in_threadpool` 또는 별도 워커로 분리한다.
- **모델·플랫폼 교체**: 모델과 플랫폼은 코드 변경 없이 프로필로 바꿀 수 있게 만든다(docs ADR-0008).
  - 서비스 코드는 인터페이스(`ImageTextEncoder`, `Detector`, VLM·임베딩 Adapter, 저장소 클라이언트, `libs/messaging`)만 쓴다. 공급자 SDK(openai, open_clip, ultralytics, boto3, Kafka 클라이언트)는 그 구현 한 곳에서만 import한다.
  - 모델 ID·차원·가중치 경로·클래스·`supported_events`·프롬프트 버전·기준값은 Git으로 관리하는 프로필 값이다. 코드에 상수로 박지 않는다.
  - 결과(이벤트, 벡터 파일, 호출 로그)마다 쓴 모델과 버전(`embedding_model`, `model_id`, `prompt_version`, `detector_profile`, `taxonomy_version`)을 싣는다.
  - `mock` Adapter는 CI·단위 테스트 전용이다. 운영 프로필에서 고르면 기동에 실패해야 한다. 모델이 없으면 결과를 지어내지 않고 `unverified`·`unknown`으로 표시한다.
- **모델 로딩**: 모델은 요청마다 로드하지 않고 기동할 때(`lifespan`, 워커는 시작 단계) 한 번 로드한다. 프로필을 검증하고, 로드에 실패하거나 출력 차원이 프로필과 다르면 기동에 실패한다. `/ready`는 로드가 끝난 뒤에만 200이다. 모델 가중치와 데이터셋은 git에 커밋하지 않고 S3 모델 저장소 경로에서 내려받는다.
- **설정**: `pydantic-settings`의 `Settings` 클래스로 환경변수를 읽는다. `os.environ`을 직접 읽지 않는다. 비밀값은 `SecretStr`로 감싼다.
- **외부 LLM·API 호출**: 타임아웃, 재시도(지수 백오프), 토큰·비용 로깅을 반드시 둔다. API 키는 로그에 남기지 않는다.
- **프롬프트**: 프롬프트는 코드에 긴 문자열로 하드코딩하지 않는다. `services/ai-gateway/prompts/`에 버전을 붙여 관리한다(`event_judge_v2.md`).
- **평가**: 모델이나 프롬프트를 바꾸면 `evals`를 실행하고, 변경 전후 지표를 PR 본문에 첨부한다.
- **로그**: `logging` 모듈로 JSON 구조화 로그를 남긴다. `print`는 금지한다. 사용자 입력 원문과 개인정보는 로그에 남기지 않는다.
- **health**: ai-gateway는 `/health`(liveness)와 `/ready`를 제공한다. 워커는 `libs/common`의 health 엔드포인트로 같은 경로를 연다.

## 테스트
- 단위 테스트에서는 외부 LLM·API를 실제로 호출하지 않는다. `respx`나 의존성 오버라이드(`app.dependency_overrides`)로 대체한다. 실제 호출은 `evals`에서만 한다.
- API 테스트는 `httpx.AsyncClient` + `ASGITransport`로 작성한다.
- 테스트 이름이나 docstring에 요구사항 ID를 포함한다: `test_spec012_r1_returns_summary` / `"""[SPEC-012 R-1] ..."""`
- 모델 출력처럼 결과가 비결정적인 부분은 정확한 문자열을 비교하지 말고 스키마와 불변 조건을 검증한다.

## CI
- PR일 때(`.github/workflows/checks.yml`): `ruff check`, `ruff format --check`, `mypy`, `pytest -m "not ffmpeg"`를 실행하고, `service-images` job이 서비스 이미지 안에서 `ffmpeg` 테스트와 runtime 빌드를 한다. 공통 검사(hadolint, actionlint, zizmor, PR 제목)는 `.claude/rules/ci-common.md`를 따른다. main 머지일 때: 바뀐 서비스(또는 그 서비스가 쓰는 `libs/`)만 이미지 빌드 → ECR push(OIDC) → gitops 레포 `envs/dev` 태그 갱신
- Dockerfile은 멀티스테이지로 만든다. uv로 의존성을 설치하고, non-root 사용자로 실행한다. 모델 가중치는 이미지에 넣지 않고 기동할 때 다운로드한다.
