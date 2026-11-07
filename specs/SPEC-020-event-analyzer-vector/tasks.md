---
spec: SPEC-020
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-020 event-analyzer 벡터 후보 판정 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- 완료한 태스크는 `[x]`로 바꾼다.

경로의 `ea/`는 `services/event-analyzer/src/event_analyzer/`, `t/`는 `services/event-analyzer/tests/`다.

## Phase 0: 준비

- [x] T001 계약 잠금 4.0.0, 단계 ID 4개 — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `vlm_contracts/stages.py`
- [x] T002 [R-10] `VLMAnalysis`·`EventAnalyzed` 모델, 벡터 방식이면 `vectorCandidate` 필수 — 테스트 `libs/contracts/tests/test_event_contracts.py`, `test_analysis_contracts.py`, 구현 `vlm_contracts/events.py`
- [x] T003 [R-20] ai-gateway 출력 스키마를 계약 `_vlm-analysis` 참조로, Registry에 `events/` — 테스트 `services/ai-gateway/tests/test_prompts.py`, 구현 `registry/prompts/event_judge/`, `prompts.py`, `registry_bundle.py`, Dockerfile
- [x] T004 [R-20] `event_judge_escalate` 등록(입출력 동일, 프로필 모델·프롬프트 버전) — 테스트 `test_prompts.py`, `test_analyze_api.py`, 구현 `registry/prompts/event_judge_escalate/`, 프로필
- [x] T005 `libs/ai-client`, `services/event-analyzer` 패키지 뼈대(pyproject, 워크스페이스 등록, mypy·pytest 대상)

## Phase 1: ai-client

- [x] T006 [R-1] `analyze` 성공: 요청 본문·`X-Caller`·`traceparent`, 응답 모델 — 테스트 `libs/ai-client/tests/test_ai_gateway_client.py`, 구현 `vlm_ai_client/client.py`
- [x] T007 [R-14] [R-15] ProblemDetail → `AiGatewayError(code, status)`, 4xx는 재시도 안 함 — 같은 테스트
- [x] T008 [R-11] 502·503·시간 초과·연결 실패는 설정 횟수까지 재시도 — 같은 테스트

## Phase 2: US-1·US-2 판정 (P1)

- [x] T009 [R-3] [R-4] 재판정 조건 — 테스트 `t/test_judgment.py`, 구현 `ea/judgment.py`
- [x] T010 [R-5] [R-6] [R-7] 최종 분석·판정 방식·검토 표시 — 테스트 `t/test_judgment.py`
- [x] T011 [R-1] [R-2] [R-8] [R-9] [R-10] 핸들러: 후보마다 `event_judge`, 후보마다 `event.analyzed`(계약 스키마 통과, 결정적 eventId) — 테스트 `t/test_analyzer_service.py`, 구현 `ea/{service,ids}.py`
- [x] T012 [R-3] 1차 high면 `event_judge_escalate`를 같은 입력으로 — 테스트 `t/test_analyzer_service.py`
- [x] T013 [R-18] 동시 판정 수 상한 — 테스트 `t/test_analyzer_service.py`
- [x] T014 [R-19] 단계 블록 `analyzer.judge`·`analyzer.escalate` — 테스트 `t/test_analyzer_service.py`

## Phase 3: US-3·US-4 실패 (P2)

- [x] T015 [R-12] [R-14] 1차 호출 실패면 `unknown`과 실패 코드 — 테스트 `t/test_analyzer_service.py`
- [x] T016 [R-13] 재판정 실패면 1차 결과와 재판정 실패 코드 — 테스트 `t/test_analyzer_service.py`
- [x] T017 [R-15] 그 밖의 4xx면 발행 없이 `NonRetryableError` — 테스트 `t/test_analyzer_service.py`
- [x] T018 [R-17] 발행 오류는 그대로 던짐 — 테스트 `t/test_analyzer_service.py`

## Phase 4: 워커와 배포

- [x] T019 기동 조립: 설정, 클라이언트, 워커 — 테스트 `t/test_analyzer_main.py`, 구현 `ea/{__main__,settings}.py`
- [x] T020 [R-15] [R-16] [NFR-1] [NFR-2] 워커 e2e: 정상 발행, 잘못된 메시지·403 DLQ, 긴 판정 중 커밋 — 테스트 `t/test_analyzer_worker_e2e.py`
- [x] T021 Dockerfile, README, CI 이미지 빌드
- [x] T022 [NFR-3] 로컬 compose 측정(docs compose PR 뒤)

## Phase 6: v5.1 추가(계약 묶음 4.0.0)

- [x] T023 [R-21] 잠금 4.0.0, ai-client 모델 재생성, `EventAnalyzed.unverified_reason`, `unverified` 최종 결과에 응답 사유 전달 — 테스트 `t/test_judgment.py`, `t/test_analyzer_service.py`, `libs/contracts/tests/test_event_contracts.py`, 구현 `ea/judgment.py`, `ea/service.py`, `vlm_contracts/events.py`, `libs/ai-client`(생성)
- [x] T024 [R-22] `EventCandidateVector.job_type` → `EventAnalyzed.job_type` 전달, 없으면 생략 — 테스트 `t/test_analyzer_service.py`, 구현 `ea/service.py`, `vlm_contracts/events.py`

## 측정 기록 (T022, 2026-11-06)

- 로컬 compose(ai-gateway mock 프로필, event-analyzer 256M): 후보 12개 메시지를 발행부터 `event.analyzed` 12건까지 0.87초(NFR-3 기준 10초)
- mock 출력이 severity `high`라 12건 모두 `vector+vlm+escalate`, 실패 코드 없음
- `event.candidate.vector`는 직접 넣었다. 근거 이미지는 실행 폴더 `blurred/`에 올린 단색 JPEG다

## 완료 조건

- [x] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [x] ruff, mypy, pytest, test-in-docker 통과
