---
spec: SPEC-013
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-013 처리 단계 측정 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `msg/`는 `libs/messaging/src/vlm_messaging/`, `con/`은 `libs/contracts/src/vlm_contracts/`다.

## Phase 1: 준비

- [x] T001 계약 잠금 2.3.0 — `service-contracts.lock`

## Phase 2: 계약 모델

- [x] T002 [R-10] [NFR-1] 단계 ID·측정 기록 모델이 계약과 같음, 봉투 `producedAt` — 테스트 `libs/contracts/tests/test_stage_contracts.py`, `test_event_contracts.py`, 구현 `con/stages.py`, `con/events.py`

## Phase 3: US-1 단계별 처리 시간 (P1)

- [x] T003 [R-8] 발행 시 `producedAt` — 테스트 `libs/messaging/tests/test_publisher.py`, 구현 `msg/publisher.py`
- [x] T004 [R-3] [R-4] 핸들러·단계 블록 기록(경로, 개수, 상태) — 테스트 `libs/messaging/tests/test_timing.py`, 구현 `msg/timing.py`
- [x] T005 [R-1] [R-2] 큐 대기(LogAppendTime 우선, `producedAt`, 둘 다 없으면 생략) — 테스트 `test_timing.py`, 구현 `msg/timing.py`
- [x] T006 [R-7] [NFR-1] [NFR-2] 결정적 ID, 측정 대상 필드, 부담 — 테스트 `test_timing.py`, 구현 `msg/timing.py`
- [x] T007 [R-5] [R-6] [R-10] 워커가 묶음을 로그·Kafka로 보냄, 출력기 실패 무시, 소비 그룹 단계 확인 — 테스트 `test_timing.py`, `test_worker.py`(Kafka), 구현 `msg/timing.py`, `msg/worker.py`
- [x] T008 [R-9] normalize 단계 블록 4개 — 테스트 `services/normalize/tests/test_service.py`, 구현 `services/normalize/src/normalize/service.py`

## Phase 4: 마무리

- [x] T009 발행 설정(normalize `STAGE_TIMING_PUBLISH`, ai-gateway `AI_GATEWAY_STAGE_TIMING_PUBLISH`), `.env.example`, AGENTS.md — 구현 `services/normalize/src/normalize/__main__.py`·`settings.py`, `services/ai-gateway/src/ai_gateway/deletion.py`·`settings.py`
- [x] T010 로컬 compose에서 normalize 로그의 측정 묶음 확인

## Phase 5: v5.1 추가(계약 묶음 4.0.0)

- [x] T011 [R-10] 잠금 4.0.0의 단계 ID 6개를 `StageId`에 추가(SPEC-023 T001·SPEC-027 T001과 같은 태스크, 먼저 하는 쪽이 한다) — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `con/stages.py`

## 완료 조건 (Definition of Done)

- [x] 이 레포가 담당하는 모든 R-*의 테스트 통과
- [x] lint, 타입 검사, 전체 테스트 통과
- [x] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
