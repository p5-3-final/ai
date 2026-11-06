---
spec: SPEC-021
repo: ai
updated: 2026-11-06
---

# Tasks: SPEC-021 내부 동기 호출 규약 적용 (ai-gateway 마감 시간, 생성 클라이언트, 재시도 규칙) — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `gw/`는 `services/ai-gateway/src/ai_gateway/`, `gwt/`는 `services/ai-gateway/tests/`, `ac/`는 `libs/ai-client/src/vlm_ai_client/`, `act/`는 `libs/ai-client/tests/`, `ea/`·`t/`는 `services/event-analyzer/src/event_analyzer/`·`tests/`다.

## Phase 0: 준비

- [x] T001 계약 잠금 4.1.0(지시로 3.14.0 대신 4.1.0): `scripts/fetch-service-contracts.sh --update 3.14.0`. 잠금을 올리면 깨지는 기존 계약 테스트를 맞춘다(단계 ID 전체 목록, `EventAnalyzed`의 `detectorCandidate`·`rule`·`rule+vlm`·`rule+vlm+escalate`·`judgments` 0개). 모델만 넓히고 SPEC-023 동작은 만들지 않는다 — 테스트 `libs/contracts/tests/test_stage_contracts.py`, `test_event_contracts.py`(지금 4건 실패가 Red), 구현 `service-contracts.lock`, `vlm_contracts/stages.py`, `vlm_contracts/events.py`
- [x] T002 datamodel-code-generator를 루트 dev 그룹에 추가(`uv add --dev`), 생성 옵션 `libs/ai-client/pyproject.toml` `[tool.datamodel-codegen]`, 스크립트 `scripts/generate-clients.sh`(shellcheck). 생성 결과의 `TaskType`(열린 목록 → `str`), nullable, 자유 객체, `extra` 처리를 확인하고 `Any`가 나오면 plan 8절대로 멈추고 확인한다
- [x] T003 [P] ai-gateway 테스트 도우미: `post_analyze`·`post_embed`·`post_parse`에 `headers` 인자, 호출을 세는 가짜 저장소·캐시·예산, 느린 가짜 Adapter — `gwt/gateway_support.py`(기존 테스트 통과로 확인)

## Phase 1: US-1 포기한 호출에 비용을 쓰지 않음 — ai-gateway (P1)

- [x] T004 [R-4] `X-Request-Deadline`이 0 이상의 정수가 아니면(빈 값, 음수, 소수, 17자리, 문자) 세 operation 모두 400 `AI_INVALID_REQUEST`, `errors[].field`가 헤더 이름 — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/deadline.py`, `gw/api.py`
- [x] T005 [R-2] 지난 마감이면 504 `AI_DEADLINE_EXCEEDED`이고 저장소·캐시·예산·Adapter 호출 수가 0, 호출 로그 행 없음 — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/api.py`(헤더 의존성), `gw/errors.py`·`gw/adapters/base.py`(`DeadlineExceededError`)
- [x] T006 [R-2] 504 본문이 계약 ProblemDetail·`DeadlineExceeded` 응답 예시와 맞음 — 테스트 `gwt/test_contract.py`
- [x] T007 [R-5] 헤더가 없으면 Adapter가 받은 `timeout_s`가 프로필 값 그대로(analyze, embed_text, parse LLM) — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/analyze.py`·`embed.py`·`parse.py`(명령 객체 `deadline: Deadline | None`)
- [x] T008 [R-1] analyze: 남은 시간이 프로필보다 짧으면 Adapter `timeout_s` = 호출 시점의 남은 시간, 길면 프로필 값 — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/deadline.py` `cap`, `gw/analyze.py`
- [x] T009 [R-1] embed_text·parse LLM 경로도 같은 규칙 — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/embed.py`, `gw/parse.py`
- [x] T010 [R-1] 게이트의 잠금 대기·분당 한도 대기가 남은 시간으로 끊기고 그때 `DeadlineExceededError`(예약 되돌림) — 테스트 `gwt/test_gate.py`, 구현 `gw/gate.py`
- [x] T011 [R-3] analyze: Adapter가 남은 시간 안에 끝나지 않으면 504, 호출 로그 status `unavailable`·`error_code` `AI_DEADLINE_EXCEEDED`, 예산은 예약액으로 정산. 프로필 제한 시간이 먼저 끝나면 지금처럼 503 — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/analyze.py`(`asyncio.timeout`, 예외 변환)
- [x] T012 [R-3] embed_text는 504, parse LLM 경로는 `safe-fallback` 대신 504(호출 로그는 남김) — 테스트 `gwt/test_deadline_api.py`, 구현 `gw/embed.py`, `gw/parse.py`
- [x] T013 [NFR-1] mock 프로필에서 지난 마감 요청 200회 p95 20ms 이하 — 테스트 `gwt/test_deadline_latency.py`
- [x] T014 [NFR-2] 기존 성능 테스트 통과 확인(`gwt/test_overhead.py`, `gwt/test_embed_latency.py`). 새 코드 없음, 결과를 PR에 적는다

## Phase 2: US-2 계약과 코드가 어긋나지 않음 — ai-client (P1)

- [x] T015 [R-7] 잠금 계약으로 `tmp_path`에 다시 생성한 결과가 커밋된 `ac/models.py`와 같아야 한다. Red는 생성 파일이 없는 상태의 비교 실패, Green은 스크립트로 생성해 커밋 — 테스트 `act/test_ai_client_generated.py`, 구현 `ac/models.py`(생성), `scripts/generate-clients.sh`
- [x] T016 [R-6] 클라이언트가 생성 모델만 쓴다: `ac/` 안에 `models.py` 밖의 `BaseModel` 하위 클래스가 없고, 계약 예시가 생성 모델로 왕복된다. 손 작성 모델 삭제, event-analyzer import를 `vlm_ai_client.models`로(`AnalyzeImage` → `ImageRef`) — 테스트 `act/test_ai_client_models.py`, 기존 `act/test_ai_gateway_client.py`·`t/test_analyzer_service.py`, 구현 `ac/client.py`, `ea/service.py`
- [x] T017 [R-13] `embed`: 요청 본문·헤더, 생성 응답 모델 — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`
- [x] T018 [R-13] `parse`: 요청 본문·헤더, 생성 응답 모델 — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`

## Phase 3: US-1·US-3 마감과 재시도 — ai-client (P1)

- [x] T019 [R-8] 모든 호출에 `X-Request-Deadline` = 첫 시도 시각 + `timeout_s`(가짜 시계), 재시도에도 같은 값, 시도별 httpx 제한 시간 = min(`timeout_s`, 남은 시간) — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`
- [x] T020 [R-9] analyze: 연결 실패(`ConnectError`·`ConnectTimeout`·`PoolTimeout`)만 설정 횟수까지 지수 대기 후 재시도, 502·503·504·응답 시간 초과·보낸 뒤 끊김은 한 번만 부른다. 기존 SPEC-020 R-11 테스트를 이 규칙으로 바꾼다 — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`(`_ANALYZE_RULE`)
- [x] T021 [R-10] embed·parse: 503과 연결 실패(보낸 뒤 끊김 포함)는 설정 횟수까지 재시도, 502·504·4xx·응답 시간 초과는 재시도 안 함 — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`(`_READ_RULE`)
- [x] T022 [R-11] 다음 대기가 끝나는 시각이 마감 이후면 대기하지 않고 마지막 오류로 끝남 — 테스트 `act/test_ai_gateway_client.py`, 구현 `ac/client.py`
- [x] T023 [R-12] 오류 코드: 504 → `AI_DEADLINE_EXCEEDED`(ProblemDetail), 코드 없는 상태 → `AI_HTTP_<상태>`, 연결 실패 → `AI_CONNECTION_FAILED`, 응답 시간 초과 → `AI_TIMEOUT`, 계약과 다른 2xx → `AI_INVALID_RESPONSE`. 기존 동작과 같은 경우는 Red 없음으로 PR에 적는다 — 테스트 `act/test_ai_gateway_client.py`

## Phase 4: event-analyzer (P1)

- [x] T024 [R-14] 클라이언트가 504 `AI_DEADLINE_EXCEEDED`를 던지면 그 판정을 실패로 기록하고 메시지를 거절하지 않으며 호출은 한 번이다. 지금 코드로도 통과할 가능성이 높아 Red 없음이면 PR에 적는다 — 테스트 `t/test_analyzer_service.py`, 구현 `ea/service.py`(필요하면)
- [x] T025 [R-9] [R-14] 조립: 재시도 설정이 analyze 규칙(연결 실패만)으로 클라이언트에 전달되고 `call_timeout_s`가 마감이 됨. 설정 주석을 "연결 실패 재시도"로 고침 — 테스트 `t/test_analyzer_main.py`, 구현 `ea/__main__.py`, `ea/settings.py`
- [x] T026 워커 e2e가 새 클라이언트로 그대로 통과(Kafka testcontainer) — 테스트 `t/test_analyzer_worker_e2e.py`

## Phase 5: 마무리

- [x] T027 계약과 구현 일치 확인: ai-gateway 응답 계약 테스트, 생성 모델 비교 테스트를 `CONTRACTS_DIR`(잠금 4.1.0)로 실행
- [x] T028 로그 확인: 504가 JSON 로그·호출 로그에 `AI_DEADLINE_EXCEEDED`로, 클라이언트 재시도 로그에 시도 번호·남은 시간
- [x] T029 README 갱신(`services/ai-gateway/README.md` 마감 헤더, `services/event-analyzer/README.md` 재시도 규칙, `libs/ai-client/pyproject.toml` description에 embed·parse·생성 방법). 환경변수 추가 없음
- [ ] T030 로컬 compose 확인(성공 지표): 지난 마감으로 `/v1/analyze`를 부르면 504이고 호출 로그에 모델 호출이 없음. 결과를 아래 "측정 기록"에 남긴다 — 보류: 로컬 compose는 `repos/ai`(main 체크아웃)에서 이미지를 빌드해 이 브랜치를 반영하지 못한다. 머지 뒤 확인한다. 같은 조건(지난 마감이면 504, 호출 로그·Adapter 호출 0)은 `gwt/test_deadline_api.py`가 검증한다

## 완료 조건 (Definition of Done)

- [ ] 이 레포가 담당하는 모든 R-*, NFR-*의 테스트 통과
- [ ] ruff check, ruff format --check, mypy, `pytest -m "not ffmpeg and not model"`, `scripts/test-in-docker.sh ai-gateway`·`event-analyzer` 통과
- [ ] plan.md와 실제 구현이 일치함(특히 생성 옵션과 재시도 표)
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
