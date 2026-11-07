---
spec: SPEC-030
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-030 매칭 규칙·성능 Gate CI 명령 (evals) — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다.
- 완료한 태스크는 `[x]`로 바꾼다.
- 이 레포 담당은 R-11, R-12, R-19뿐이다. 선행: SPEC-021, SPEC-027 T005(`vlm_common.http_call`).

경로의 `ev/`는 `evals/src/evals/`, `et/`는 `evals/tests/`다.

## Phase 0: 준비

- [x] T001 계약 잠금을 5.0.0(quality-service 3.0.0, ADR-0018)으로 올리고 생성 클라이언트를 모두 다시 만듦(`scripts/fetch-service-contracts.sh --update 5.0.0`, `scripts/generate-clients.sh`) — `service-contracts.lock`, `libs/*-client/src/*/models.py`
- [x] T002 `evals` 멤버 뼈대(`vlm-evals`, 패키지 `evals`, 워크스페이스·mypy·pytest 등록, `NotImplementedError` 인터페이스) — `evals/pyproject.toml`, 루트 `pyproject.toml`

## Phase 1: US-2 매칭 규칙 (P1)

- [x] T003 [R-11] temporal IoU(겹친 길이 / 합친 길이, 길이 0이면 0) — 테스트 `et/test_evals_matching.py`, 구현 `ev/matching.py`
- [x] T004 [R-11] 같은 유형끼리, 기준 이상 쌍을 IoU 큰 순(같으면 라벨 시작 이른 순) 일대일, 놓침·오탐 — 테스트 `et/test_evals_matching.py`
- [x] T005 [P] [R-12] bbox: 같은 클래스, 시각 차 ≤ 프레임 간격 절반, 박스 IoU 기준, 같은 순서 규칙 — 테스트 `et/test_evals_matching.py`
- [x] T006 [R-12] 상태 변화: 같은 클래스·같은 종류(대응표 인자, plan 5절 1 기본안), 시각 차 기준, 가까운 순 일대일 — 테스트 `et/test_evals_matching.py`

## Phase 2: US-4 성능 Gate 명령 (P2)

- [x] T007 [R-19] `quality-service.yaml` 생성 모델과 생성 코드 비교 — 구현 `libs/quality-client/src/vlm_quality_client/models.py`(생성), `scripts/generate-clients.sh` 표 한 줄, 테스트 `libs/quality-client/tests/test_quality_client_generated.py`
- [x] T008 [R-19] `evaluateStageGate` 호출: 요청 본문·`X-Caller`, 503·연결 실패 2회 재시도, 응답 검증 — 테스트 `libs/quality-client/tests/test_quality_client.py`, 구현 `libs/quality-client/src/vlm_quality_client/client.py`
- [x] T009 [R-19] CLI 종료 코드(`passed` true 0, false 1, 호출·입력 오류 2)와 단계별 JSON 로그 — 테스트 `et/test_evals_stage_gate.py`
- [x] T010 README(명령, 환경변수). `.env.example`은 권한 설정에 막혀 README 환경변수 표에만 적음

## 완료 조건

- [x] R-11, R-12, R-19 테스트 통과
- [ ] ruff, mypy, pytest 통과
- [x] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신(backend 구현과 함께 `Implementing` / `Done`)
