---
spec: SPEC-007
repo: ai
updated: 2026-11-03
---

# Tasks: SPEC-007 영상 정규화 — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

## Phase 1: 준비

- [x] T001 `services/normalize` 멤버 골격(pyproject, 패키지, 설정), 루트 workspace·mypy 대상 추가 — `services/normalize/`, `pyproject.toml` (806692d)
- [x] T002 [R-6] 계약 모델 `VideoNormalizing`, `VideoNormalized` 추가와 계약 테스트 등록 — 테스트 `libs/contracts/tests/`, 구현 `libs/contracts/src/vlm_contracts/events.py` (806692d)
- [x] T003 [P] [R-22] [R-23] 워커 health 서버(`/health` 최근 beat, `/ready`) — 테스트 `libs/common/tests/test_health.py`, 구현 `libs/common/src/vlm_common/health.py` (806692d)
- [x] T004 [R-23] 워커가 루프마다 beat, 구독 뒤 ready 표시 — 테스트 `libs/messaging/tests/test_worker.py`, 구현 `libs/messaging/src/vlm_messaging/worker.py` (806692d)
- [x] T005 Dockerfile(`runtime`, `test`)과 컨테이너 안 테스트 실행 스크립트, testcontainers 주소 설정 — `services/normalize/Dockerfile`, `scripts/test-in-docker.sh` (7d1ae15)

## Phase 2: US-1·US-3 정규화 성공 (P1)

- [x] T006 [R-14] 저장 경로와 이벤트 ID 결정 — 테스트 `services/normalize/tests/test_ids.py`, 구현 `normalize/ids.py` (ceebe9f)
- [x] T007 [R-7] ffprobe 출력 파싱, 실패는 `MediaReadError` — 테스트 `test_probe.py`, 구현 `normalize/probe.py` (ceebe9f)
- [x] T008 [R-3] [R-4] 복사·재인코딩 판정과 fps 상한 — 테스트 `test_decision.py`, 구현 `normalize/decision.py` (ceebe9f)
- [x] T009 [R-1] 검증 전에 `video.normalizing` 발행 — 테스트 `test_service.py`, 구현 `normalize/service.py` (7277966)
- [x] T010 [R-2] [R-5] [R-6] 정규화 사본·HLS 저장 후 READY 발행(경로, 길이, fps, 해상도, 재인코딩 여부) — 테스트 `test_service.py`, 구현 `normalize/service.py` (7277966)
- [x] T011 [R-3] [R-4] [R-5] `FfmpegTool` 복사·재인코딩·HLS 실제 동작 — 테스트 `test_ffmpeg.py`(Docker), 구현 `normalize/ffmpeg.py` (264aea9)

## Phase 3: US-2 거절 (P1)

- [x] T012 [R-8] [R-9] [R-10] [R-11] 거절 판정 — 테스트 `test_decision.py`, 구현 `normalize/decision.py` (ceebe9f)
- [x] T013 [R-7] [R-12] 열 수 없음·디코딩 실패 시 REJECTED 발행 — 테스트 `test_service.py`, 구현 `normalize/service.py` (7277966)
- [x] T014 [R-7] [R-8] [R-10] [R-12] 실제 파일(깨진 파일, 음성만, 0.5초, 디코딩 불가)로 거절 확인 — 테스트 `test_ffmpeg.py`(Docker) (264aea9)
- [x] T015 [R-13] 저장소 오류는 거절하지 않고 예외로 재시도 — 테스트 `test_service.py` (7277966)

## Phase 4: 운영 (P2)

- [x] T016 [NFR-4] [NFR-5] 로그에 파일 이름 없음, 작업 폴더 삭제 — 테스트 `test_service.py` (7277966)
- [x] T017 [R-13] [R-23] 워커 E2E: Kafka·SeaweedFS로 `video.uploaded` → 두 이벤트, 저장 경로 확인 — 테스트 `test_worker_e2e.py`(Docker), 구현 `normalize/__main__.py` (2914bb7)

## Phase 5: 마무리

- [x] T018 CI: hadolint, normalize 이미지 `test` 단계 실행 job — `.github/workflows/checks.yml` (d566fad)
- [x] T019 [NFR-1] [NFR-2] 성능 측정 스크립트와 결과 기록 — `scripts/bench_normalize.sh` (aaa53b9)
- [x] T020 환경변수 예시(`.env.example`), AGENTS 명령 갱신. 로컬 compose·`local/seaweedfs/s3.json`은 docs 브랜치 `feat/SPEC-007-local-normalize`에 두고 ai를 main에 합칠 때 함께 머지

## 완료 조건 (Definition of Done)

- [ ] 이 레포가 담당하는 모든 R-*의 테스트 통과
- [ ] lint, 타입 검사, 전체 테스트 통과(로컬 + Docker)
- [ ] plan.md와 실제 구현이 일치함
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
