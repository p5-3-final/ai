---
spec: SPEC-012
repo: ai
updated: 2026-11-06
---

# Tasks: SPEC-012 ai-gateway 질의 해석 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `gw/`는 `services/ai-gateway/src/ai_gateway/`, `t/`는 `services/ai-gateway/tests/`, `reg/`는 `services/ai-gateway/registry/`다.

## Phase 1: 준비

- [ ] T001 계약 잠금 2.7.0 — `service-contracts.lock`
- [ ] T002 스키마 읽기·참조·분류 체계 버전 함수 공개(동작 불변, 기존 테스트로 확인) — `gw/prompts.py`

## Phase 2: 질의 사전 (US-1, US-2, US-3, US-7)

- [ ] T003 [R-23] [R-24] [R-29] [R-30] 사전 로드·검증(스키마, 표현 중복, 정규형), 버전 `<이름>@<해시>`, 분류 체계 버전 — 테스트 `t/test_query_dictionary.py`, 구현 `gw/query_dictionary.py`, `reg/query/`
- [ ] T004 [R-2] 정규화(공백, 소문자, 불용 표현) — 테스트 `t/test_query_dictionary.py`, 구현 `gw/query_dictionary.py`
- [ ] T005 [R-3] [R-4] route — 테스트 `t/test_query_dictionary.py`, 구현 `gw/query_dictionary.py`
- [ ] T006 [R-5] [R-6] [R-7] 사전 매칭(긴 표현 우선, 사전 순서, 대상어 3개, 이벤트 유형) — 테스트 `t/test_query_dictionary.py`, 구현 `gw/query_dictionary.py`
- [ ] T007 [R-8] 영어 위주 판정과 영어 구문 — 테스트 `t/test_query_dictionary.py`, 구현 `gw/query_dictionary.py`
- [ ] T008 [NFR-4] 사전 v1 내용과 예시 세트 — 테스트 `t/test_query_dictionary.py`, 구현 `reg/query/dictionary.json`

## Phase 3: 프로필과 Registry (US-4)

- [ ] T009 [R-28] `[parse]` 프로필, Adapter 참조, mock 허용 검사 — 테스트 `t/test_profile.py`, 구현 `gw/profile.py`
- [ ] T010 [R-10] `query_parse` 프롬프트 폴더, `/v1/analyze`는 `query_parse`를 모르는 taskType으로 거절 — 테스트 `t/test_prompts.py`, `t/test_analyze_api.py`, 구현 `reg/prompts/query_parse/`, `gw/analyze.py`, `gw/main.py`(`check_tasks`)
- [ ] T011 [R-10] Adapter `temperature` 인자 — 테스트 `t/test_openai_adapter.py`, 구현 `gw/adapters/`

## Phase 4: `/v1/parse` (US-1~US-6)

- [ ] T012 [R-1] [R-5] [R-16] [R-17] [R-19] 사전 해석 응답 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`, `gw/api.py`, `gw/main.py`
- [ ] T013 [R-18] 표준 질의 문자열 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`
- [ ] T014 [R-8] [R-9] 영어 구문과 빈 구문의 안전 대체 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`
- [ ] T015 [R-10] [R-11] [R-15] [R-26] LLM 보조, route는 사전으로만, 사전 해석은 Adapter 미호출 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`
- [ ] T016 [R-12] [R-13] [R-14] 모델 미설정·공급자 오류·출력 검증 실패의 안전 대체 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`
- [ ] T017 [R-20] [R-21] [R-22] 빈 문장·길이 상한·호출자 거부 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`, `gw/api.py`
- [ ] T018 [R-27] [NFR-3] 호출 로그와 애플리케이션 로그에 원문 없음 — 테스트 `t/test_parse_api.py`, `t/test_call_log.py`(PostgreSQL), 구현 `gw/parse.py`
- [ ] T019 [R-1] 응답이 계약 `ParseResponse`와 맞음 — 테스트 `t/test_contract.py`
- [ ] T020 [NFR-1] [NFR-2] 사전 해석 p95, LLM 제한 시간 전달 — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py`

## Phase 5: 기동 (US-7)

- [ ] T021 [R-25] [R-31] 사전 로드가 기동 안에서 끝나고, 사전·출력 스키마 분류 체계 버전이 다르면 기동 실패 — 테스트 `t/test_startup.py`, 구현 `gw/main.py`
- [ ] T022 Registry 묶음·이미지에 `query/` — 테스트 `t/test_registry_bundle.py`, 구현 `gw/registry_bundle.py`, `Dockerfile`, `t/conftest.py`
- [ ] T023 로컬 프로필 `[parse]`, AGENTS.md — `profiles/`, `services/ai-gateway/AGENTS.md`(있으면)

## Phase 6: 마무리

- [ ] T024 로컬 compose: 사전 해석 → `embed_visual_text`, Ollama 정지 시 `industrial-rule`·`safe-fallback`, LLM 보조

## 완료 조건 (Definition of Done)

- [ ] 이 레포가 담당하는 모든 R-*의 테스트 통과
- [ ] lint, 타입 검사, 전체 테스트 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
