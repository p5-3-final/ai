---
spec: SPEC-009
repo: ai
updated: 2026-11-03
---

# Tasks: SPEC-009 ai-gateway 분석 호출 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `gw/`는 `services/ai-gateway/src/ai_gateway/`, `t/`는 `services/ai-gateway/tests/`다.

## Phase 1: 준비

- [x] T001 workspace 멤버 추가, 의존성 설치, 빈 패키지·설정(`settings.py`) — `services/ai-gateway/pyproject.toml`, 루트 `pyproject.toml`
- [x] T002 Alembic 환경과 `ai.ai_call_log` 마이그레이션 — `services/ai-gateway/migrations/`, 테스트 `t/test_migrations.py`(PostgreSQL)

## Phase 2: 기동 검증 (US-3, US-6)

- [x] T003 [R-14] 프로필 스키마와 검증(허용 호출자·이미지 패턴·Adapter·taskType 설정) — 테스트 `t/test_profile.py`, 구현 `gw/profile.py`
- [x] T004 [R-15] mock 허용 설정 없이 mock Adapter를 고르면 기동 실패 — 테스트 `t/test_profile.py`, 구현 `gw/profile.py`
- [ ] T005 [R-13] Prompt Registry: 계약 폴더 읽기, 분류 체계 $ref 해석, 내용 해시 버전 ID — 테스트 `t/test_prompts.py`, 구현 `gw/prompts.py`
- [ ] T006 [R-12] 고정 접두부와 가변부 분리 — 테스트 `t/test_prompts.py`, 구현 `gw/prompts.py`

## Phase 3: US-1 이벤트 후보 판정 (P1)

- [ ] T007 [R-5] 응답에서 JSON 추출과 출력 스키마 검증 — 테스트 `t/test_output.py`, 구현 `gw/output.py`
- [ ] T008 [R-6] [R-7] 추출·검증 실패 시 `unknown` 결과(위험 필드 규칙 포함) — 테스트 `t/test_output.py`, 구현 `gw/output.py`
- [ ] T009 [R-17] [NFR-6] 호출 로그 저장(uuid7 호출 ID) — 테스트 `t/test_call_log.py`, 구현 `gw/call_log.py`
- [ ] T010 [R-1] `/v1/analyze` 성공 흐름(가짜 Adapter, 메모리 저장소) — 테스트 `t/test_analyze_api.py`, 구현 `gw/api.py`, `gw/analyze.py`, `gw/main.py`
- [ ] T011 [R-2] [R-3] 요청 형식·입력 변수·모르는 taskType 거절(ProblemDetail) — 테스트 `t/test_analyze_api.py`, 구현 `gw/api.py`
- [ ] T012 [R-4] 이미지 상한 초과 거절 — 테스트 `t/test_analyze_api.py`, 구현 `gw/analyze.py`
- [ ] T013 [R-11] 저장소에 없는 이미지 거절 — 테스트 `t/test_analyze_api.py`, 구현 `gw/images.py`
- [ ] T014 [R-6] 출력 검증 실패 시 200 `unknown`과 `invalid_output`·원문 기록 — 테스트 `t/test_analyze_api.py`, 구현 `gw/analyze.py`
- [ ] T015 [R-8] 모델 미설정이면 호출 없이 `unverified` — 테스트 `t/test_analyze_api.py`, 구현 `gw/analyze.py`

## Phase 4: US-2 원본 영상 보호 (P1)

- [ ] T016 [R-9] 허용 패턴 밖 경로 거절(원본·정규화 사본·HLS 포함) — 테스트 `t/test_images.py`, `t/test_analyze_api.py`, 구현 `gw/images.py`
- [ ] T017 [R-10] 다른 영상 폴더·`..` 경로 거절 — 테스트 `t/test_images.py`, 구현 `gw/images.py`

## Phase 5: US-4 호출 추적·장애 구분 (P2)

- [ ] T018 [R-16] 호출자 헤더 없음·허용 목록 밖 거절 — 테스트 `t/test_analyze_api.py`, 구현 `gw/api.py`
- [ ] T019 [R-18] [R-19] openai Adapter: 메시지 구성, 일시 오류·거부 구분, 토큰 수 — 테스트 `t/test_openai_adapter.py`(respx), 구현 `gw/adapters/openai.py`
- [ ] T020 [R-18] [R-19] API에서 503·502와 로그 상태 — 테스트 `t/test_analyze_api.py`, 구현 `gw/analyze.py`, `gw/api.py`
- [ ] T021 [NFR-5] `traceparent` 이어 받기와 공급자 호출 헤더 — 테스트 `t/test_openai_adapter.py`, 구현 `gw/tracing.py`
- [ ] T022 [NFR-3] 로그에 이미지·원문·키 없음 — 테스트 `t/test_analyze_api.py`(caplog), 구현 확인
- [ ] T023 [P] mock Adapter — 테스트 `t/test_mock_adapter.py`, 구현 `gw/adapters/mock.py`

## Phase 6: US-5 삭제 전파 (P2)

- [ ] T024 [R-20] [R-21] `video.deleted` 핸들러: 원문 삭제, 반복 수신 — 테스트 `t/test_deletion.py`, 구현 `gw/deletion.py`
- [ ] T025 [R-20] Kafka 소비 연결(백그라운드 스레드, 별도 DB 엔진) — 테스트 `t/test_deletion_e2e.py`(integration), 구현 `gw/deletion.py`, `gw/main.py`

## Phase 7: US-6 배포 상태 (P2)

- [ ] T026 [R-22] [R-23] `/health`, `/ready`(기동 검증·DB·구독) — 테스트 `t/test_health.py`, 구현 `gw/main.py`

## Phase 8: 마무리

- [ ] T027 [NFR-1] 자체 처리 시간 p95 측정 — 테스트 `t/test_overhead.py`(integration)
- [ ] T028 [NFR-2] [NFR-4] 공급자 SDK import 위치, API 키 `SecretStr` — 테스트 `t/test_architecture.py`, `t/test_profile.py`
- [ ] T029 계약 일치: 요청·응답 모델과 `ai-gateway.yaml`, 프롬프트 예시 — 테스트 `t/test_contract.py`
- [ ] T030 Dockerfile(계약 빌드 컨텍스트), CI service-images, 로컬 프로필, `.env.example`, AGENTS.md(프롬프트 위치), README

## 완료 조건 (Definition of Done)

- [ ] 이 레포가 담당하는 모든 R-*의 테스트 통과
- [ ] lint, 타입 검사, 전체 테스트 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
