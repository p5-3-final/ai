---
spec: SPEC-016
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-016 ai-gateway 결과 캐시·예산·호출 한도 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `gw/`는 `services/ai-gateway/src/ai_gateway/`, `t/`는 `services/ai-gateway/tests/`다.

## Phase 1: 준비

- [x] T001 의존성 `redis`, 계약 잠금 2.8.0, Redis 테스트 컨테이너 — `services/ai-gateway/pyproject.toml`, `service-contracts.lock`, `libs/testing/src/vlm_testing/containers.py` (d77883d)
- [x] T002 마이그레이션 0004(캐시·예산 테이블, 호출 로그 컬럼) — 테스트 `t/test_migrations.py`(PostgreSQL), 구현 `gw/db/` (d77883d)

## Phase 2: 프로필 (US-3, US-4)

- [x] T003 [R-20] [R-21] `[cache]`·`[budget]`·`[prices]`·`[rate_limits]`·예상 토큰 검증 — 테스트 `t/test_profile.py`, 구현 `gw/profile.py` (ce686f3)

## Phase 3: 저장소 (US-1, US-3, US-4, US-5, US-6)

- [x] T004 [R-1] [R-2] [R-6] [R-9] [R-17] 결과 캐시(DB·Redis 앞단, 보관 기간, 영상별 삭제, Redis 끊김 시 DB만) — 테스트 `t/test_result_cache.py`(PostgreSQL·Redis), 구현 `gw/result_cache.py` (b01b174)
- [x] T005 [R-7] 동시 호출 잠금 — 테스트 `t/test_result_cache.py`(Redis), 구현 `gw/result_cache.py` (b01b174)
- [x] T006 [R-10] [R-11] [R-12] [R-13] [R-14] [R-21] [R-22] [NFR-2] 예산 예약·정산·되돌림, 동시 예약 100건 — 테스트 `t/test_budget.py`(PostgreSQL), 구현 `gw/budget.py` (5122875)
- [x] T007 [R-15] [R-16] [R-17] 분당 한도(Redis 창, Pod별 대체, 마감 넘으면 오류) — 테스트 `t/test_rate_limit.py`, 구현 `gw/rate_limit.py` (e90ace3)

## Phase 4: 게이트와 경로 (US-1~US-4, US-7)

- [x] T008 [R-1] [R-2] [R-3] [R-7] [R-8] [R-10]~[R-13] [R-16] [R-22] `ModelGate` 순서와 결과 종류 — 테스트 `t/test_gate.py`, 구현 `gw/gate.py`, `gw/adapters/`(`sent`), `gw/errors.py` (6721b0b)
- [x] T009 [R-4] [R-11] [R-19] `/v1/analyze` 게이트 사용(이미지 내용 키, 예산 초과 `unverified`, 호출 로그) — 테스트 `t/test_analyze_api.py`, `t/test_call_log.py`, 구현 `gw/analyze.py`, `gw/call_log.py` (ac3f46d)
- [x] T010 [R-11] [R-19] `/v1/parse` LLM 경로 게이트 사용(예산 초과 `safe-fallback`) — 테스트 `t/test_parse_api.py`, 구현 `gw/parse.py` (98c4bb3)
- [x] T011 [R-5] [R-11] [R-19] `embed_text` 문장별 캐시, 예산 초과 503 — 테스트 `t/test_embed_api.py`, 구현 `gw/embed.py` (b6faf59)
- [x] T012 [R-9] `video.deleted`가 캐시를 지움 — 테스트 `t/test_deletion.py`, 구현 `gw/deletion.py` (416ae4d)

## Phase 5: 조립 (US-5)

- [x] T013 [R-17] [R-18] 운영 조립, Redis 설정, `/ready`는 Redis와 무관 — 테스트 `t/test_startup_e2e.py`, 구현 `gw/main.py`, `gw/settings.py`, `.env.example`, `profiles/` (f0b07eb)
- [x] T014 [P] 계약 대조(`AI_BUDGET_EXCEEDED`) — 테스트 `t/test_contract.py` (f0b07eb)
- [x] T015 로컬 compose: 같은 analyze 두 번, 작은 예산, Redis 정지, NFR-1·NFR-4 측정 — docs `local/`(Redis 연결 설정) — 같은 analyze 두 번(적중, 결과 같음), 작은 예산(analyze budget_exceeded·parse safe-fallback), Redis 정지(/ready 200, DB 캐시 적중, 경고 1회). p95: 적중 8.0ms·미적중 6.5ms(이미지 읽기 포함, mock 모델), parse LLM 적중 2.1ms

## Phase 7: v5.1 추가(계약 묶음 4.0.0)

- [x] T016 [R-23] `unverified` 응답에 `unverifiedReason`(`model_unavailable`·`budget_exceeded`), 그 밖에는 싣지 않음 — 테스트 `t/test_analyze_api.py`, `t/test_contract.py`, 구현 `gw/analyze.py`, `gw/api.py`

## 완료 조건 (Definition of Done)

- [x] 이 레포가 담당하는 모든 R-*의 테스트 통과
- [x] lint, 타입 검사, 전체 테스트 통과
- [x] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
