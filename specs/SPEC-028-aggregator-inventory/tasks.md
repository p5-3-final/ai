---
spec: SPEC-028
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-028 aggregator 정적 객체 재고 비교·장비 방치·위험 노출 예측 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- 완료한 태스크는 `[x]`로 바꾼다.
- 선행: SPEC-021, SPEC-027 tasks 완료. `(확인 대기)` 표시는 plan 5절 사용자 확인 뒤에 한다.

경로 약칭: `agg/` `services/aggregator/src/aggregator/`, `t/` `services/aggregator/tests/`, `gw/` `services/ai-gateway/src/ai_gateway/`, `gt/` `services/ai-gateway/tests/`.

## Phase 0: 준비

- [x] T001 계약 잠금 확인(4.1.0, SPEC-021에서 이미 올림. `_risk-forecast.horizons` 최소 0개가 들어 있다) — `service-contracts.lock`
- [x] T002 [R-18] 단계 ID 4개(`aggregate.inventory_store`·`inventory_compare`·`judge`·`forecast`) — 테스트 `libs/contracts/tests/test_stage_contracts.py`, 구현 `vlm_contracts/stages.py`
- [x] T003 [P] [R-1] `ChunkManifest`·`ChunkInventory` 모델(계약 비교, 예시 왕복) — 테스트 `libs/contracts/tests/test_event_contracts.py`, `test_aggregate_contracts.py`, 구현 `vlm_contracts/{events,aggregates}.py`
- [x] T004 [P] [R-13] `SourceResult` 확장 필드·타임라인 `inventory_event`, `SourceCompleted.counts` 선택 필드 — 같은 테스트
- [x] T005 [P] [R-2] 저장소 `list_keys(prefix)`(S3·메모리 같은 결과) — 테스트 `libs/storage/tests/test_storage.py`, 구현 `vlm_storage/{storage,s3,memory}.py`

## Phase 1: 재고 저장

- [x] T006 [R-1] `chunk.manifest`면 청크 재고 파일을 없을 때만 저장, 있으면 아무것도 안 함 — 테스트 `t/test_aggregator_inventory_store.py`, 구현 `agg/inventory_store.py`
- [x] T007 [R-2] 재고 파일 수 < `counts.chunks`면 발행 없이 재시도 가능 오류 — 테스트 `t/test_aggregator_reader.py`, 구현 `agg/reader.py`

## Phase 2: US-1 이어 맞추기와 변화 후보 (P1)

- [x] T008 [R-3] 소유 범위 순, 같은 클래스 IoU 기준 이상 쌍을 큰 순서로 하나씩 짝지어 사슬, objectId 규칙 — 테스트 `t/test_aggregator_inventory.py`, 구현 `agg/inventory.py`
- [x] T009 [R-4] 짝 없는 앞 객체 `removed`(직전 시각), 뒤 객체 `appeared`(이후 시각) — 테스트 `t/test_aggregator_inventory.py`
- [x] T010 [R-5] 같은 클래스 제거·등장이 이동 창 안이면 가까운 쌍부터 `moved` — 테스트 `t/test_aggregator_inventory.py`
- [x] T011 [R-13] 영상 끝 재고 = 마지막 청크 객체의 사슬 — 테스트 `t/test_aggregator_inventory.py`
- [x] T012 [R-17] 변화·재고 이벤트 ID 결정성 — 테스트 `t/test_aggregator_ids.py`, 구현 `agg/ids.py`

## Phase 3: US-1 판정 (P1)

- [x] T013 [R-7] ai-gateway `state_compare` 등록(입출력 스키마, 자리 표시 프롬프트, `fallback.json`, 두 프로필, 도우미, 계약 파일 배치) — 테스트 `gt/test_prompts.py`, `gt/test_analyze_api.py`, 구현 `registry/prompts/state_compare/`, 프로필, `tests/conftest.py`, Dockerfile
- [x] T014 [R-7] 후보마다 이미지 두 장·변수로 `state_compare`, `appeared`는 호출 없이 기록 — 테스트 `t/test_aggregator_judge.py`, 구현 `agg/judge.py`
- [x] T015 [R-8] 직전 시각 순 판정, 상한에서 멈추고 남은 후보는 `eventId`·`verificationStatus` null — 테스트 `t/test_aggregator_judge.py`
- [x] T016 [R-9] 유형별 재고 이벤트, `confirmed`면 상태·노출·타임라인(`inventory_event`)에 들어감 — 테스트 `t/test_aggregator_judge.py`, `t/test_aggregator_compute.py`, 구현 `agg/compute.py`
- [x] T017 [R-10] 호출 오류면 `unknown` 분석으로 기록, 상태 계산 제외 — 테스트 `t/test_aggregator_judge.py`

## Phase 4: US-2 미복구 (P1)

- [x] T018 [R-6] (plan 5절 1 A안: 직전 프레임을 이 job 폴더로 복사) 직전 끝 재고를 첫 청크 앞에 놓고, 방호 클래스 미짝 `removed`를 `not_restored`로, 판정 이미지 경로 — 테스트 `t/test_aggregator_inventory.py`, `t/test_aggregator_judge.py`

## Phase 5: US-3 장비 방치 (P2)

- [x] T019 [R-11] 장비 클래스가 방치 기준 이상 머물고 같은 구역에 사람이 없으면 `equipment_abandoned`, 구역 없으면 제외 — 테스트 `t/test_aggregator_equipment.py`, 구현 `agg/equipment.py`
- [x] T020 [R-12] `aisle`·`hazard`면 `event_judge`로 `rule+vlm`(상한에 셈), 아니면 템플릿 `rule`·`confirmed` — 테스트 `t/test_aggregator_judge.py`

## Phase 6: US-4 예측 (P3)

- [x] T021 [R-16] `predictor` Adapter 종류·프로필 검증·규칙 점수(결정적, 0~1, 두 입력만 사용) — 테스트 `gt/test_predictor_adapter.py`, `gt/test_profile.py`, 구현 `gw/adapters/predictor.py`, `gw/profile.py`
- [x] T022 [R-16] (plan 5절 2 A안: 계약 4.1.0 `horizons` 최소 0개) `risk_forecast` 등록과 `/v1/analyze` predictor 경로(게이트 없음, 출력 검증, 호출 로그) — 테스트 `gt/test_analyze_api.py`, `gt/test_prompts.py`, 구현 `gw/{analyze,main}.py`, `registry/prompts/risk_forecast/`, 프로필
- [x] T023 [NFR-3] `risk_forecast` p95 20ms — 테스트 `gt/test_overhead.py`
- [x] T024 [R-14] 영상 끝 활성 위험마다 변수(구역 분당 진입 수 포함)로 호출, `forecasts` — 테스트 `t/test_aggregator_forecast.py`, 구현 `agg/forecast.py`
- [x] T025 [R-15] 미설정이면 `not_configured`, 오류면 `failed`, 나머지 발행 — 테스트 `t/test_aggregator_forecast.py`, `t/test_aggregator_service.py`

## Phase 7: 마무리

- [x] T026 [R-18] 단계 블록 4개와 count — 테스트 `t/test_aggregator_service.py`, `t/test_aggregator_inventory_store.py`
- [x] T027 [R-17] 같은 입력·프로필이면 판정·예측 결과를 뺀 결과 파일이 같음(입력 순서를 섞어도) — 테스트 `t/test_aggregator_compute.py`
- [x] T028 [NFR-1] 청크 60 × 객체 100 이어 맞추기 p95 2초 — 테스트 `t/test_aggregator_performance.py`
- [x] T029 [R-1] [R-9] 워커에 `chunk.manifest` 경로, e2e(`chunk.manifest` 두 건 + `analysis.completed` → `object_moved`가 든 `source.completed`) — 테스트 `t/test_aggregator_worker_e2e.py`, 구현 `agg/__main__.py`
- [ ] T030 [NFR-2] 로컬 compose 토픽(docs PR)과 `chunk.manifest` 처리 p95 측정, README 갱신, 아래에 측정 기록

## Phase 8: v5.1 추가(계약 묶음 4.0.0)

- [ ] T031 [R-19] `ChunkManifest` 4.0.0 선택 필드, `STREAM` 재고 파일 저장, `inventory_due`와 주기 계산(재고 비교·장비 방치·예측), 확정 재고 이벤트를 다음 창 fold 입력으로 — 테스트 `libs/contracts/tests/test_event_contracts.py`, `t/test_aggregator_stream_inventory.py`, 구현 `vlm_contracts/events.py`, `agg/stream.py`, `agg/profile.py`
- [ ] T032 [R-19] 세션당 판정 상한을 Redis 계수로(SPEC-027 T053과 같은 계수), 업로드는 기존 계수 유지 — 테스트 `t/test_aggregator_judge_budget.py`(integration), 구현 `agg/judge_budget.py`, `agg/judge.py`
- [ ] T033 [R-19] 회전 세션의 직전 끝 재고를 첫 청크 앞에 놓기, 세션 마무리의 마지막 계산과 `forecasts` — 테스트 `t/test_aggregator_stream_inventory.py`, 구현 `agg/stream.py`

## 측정 기록

(T030에서 채운다)

## 완료 조건

- [ ] 모든 R-*, NFR-* 테스트 또는 측정 통과
- [ ] ruff, mypy, pytest, test-in-docker 통과
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신(`Implementing` / `Done`)
