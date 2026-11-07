---
spec: SPEC-022
repo: ai
updated: 2026-11-07
---

# Tasks: SPEC-022 track-stitcher 경계 트랙 연결과 재등장 연결(Re-ID) — ai

규칙

- 태스크 하나는 커밋 하나에서 PR 하나 사이의 크기로 잡는다(반나절 이하).
- `[P]`는 다른 태스크와 병렬로 진행할 수 있다는 뜻이다. `[R-n]`은 담당 요구사항이다.
- 태스크 하나는 요구사항 하나의 Red → Green → Refactor 사이클이다. 테스트와 구현을 같은 태스크에서 끝내서, 커밋마다 테스트가 통과하는 상태를 유지한다.
- 완료한 태스크는 `[x]`로 바꾼다. 커밋한 뒤에는 커밋 해시를 덧붙인다.

경로의 `ts/`는 `services/track-stitcher/src/track_stitcher/`, `t/`는 `services/track-stitcher/tests/`, `ct/`는 `libs/contracts/tests/`다.

선행 조건: plan 8절 1·2번은 계약 4.1.0(docs #137)에서 해결됐고 잠금도 4.1.0이다. 3번은 D-7(끄기 설정 없음), 4~7번은 plan의 기본안으로 구현했다.

## Phase 0: 준비

- [x] T001 계약 잠금 4.0.0(확인만 했다. 잠금은 이미 4.1.0이다): `scripts/fetch-service-contracts.sh --update 4.0.0`(SPEC-021 T001이 먼저 끝났으면 버전·단계 ID만 확인). 8절 계약 수정이 나오면 그 버전으로 다시 올린다 — 테스트 `ct/test_stage_contracts.py`, 구현 `service-contracts.lock`, `vlm_contracts/stages.py` (b436d53)
- [x] T002 [R-1] `ChunkBoundary` 이벤트 모델과 트랙 요약 파일 모델 `ChunkTracks`(경계 박스 좌표 0~1·`x1<x2`·`y1<y2`, 대표 크롭) — 테스트 `ct/test_event_contracts.py`(`EVENT_MODELS`에 추가), `ct/test_track_file_contracts.py`, 구현 `vlm_contracts/events.py`, `vlm_contracts/tracks.py` (b436d53)
- [x] T003 [P] [R-5] `TrackDetected` 모델: origin `chunk`·`stitcher`별 필수 필드, `stitched[].members` 2개 이상 — 테스트 `ct/test_event_contracts.py`, 구현 `vlm_contracts/events.py` (b436d53)
- [x] T004 [P] [R-12] [R-13] `TrackManifest` 이벤트와 번역표 파일 모델 `TrackMapping`(links의 `gapMs`는 reid만) — 테스트 `ct/test_event_contracts.py`, `ct/test_track_file_contracts.py`, 구현 `vlm_contracts/events.py`, `vlm_contracts/tracks.py` (b436d53)
- [x] T005 `services/track-stitcher` 뼈대: pyproject(의존성 `vlm-common`·`vlm-contracts`·`vlm-messaging`·`vlm-storage`·`redis`), 워크스페이스 `members`, `[tool.mypy] files`·`mypy_path`, pytest `pythonpath`, `t/stitcher_support.py`(트랙 파일·벡터 파일 만드는 도우미) (94eeb4e)
- [x] T006 [R-22] 프로필 스키마와 `profiles/local.toml`(자리값 주석). 없는 파일, IoU·유사도 경계 0~1 밖, 간격·대기 제한 시간 0 이하, 최소 공통 관측 1 미만이면 `ProfileError` — 테스트 `t/test_stitcher_profile.py`, 구현 `ts/profile.py` (94eeb4e)

## Phase 1: US-1 청크 경계에서 끊기지 않는 트랙 (P1)

- [x] T007 [P] [R-3] 인접 판단: 앞 `ownedRange.endMs` = 뒤 `ownedRange.startMs`인 쌍만, 틈·겹침은 인접 아님 — 테스트 `t/test_stitcher_boundary.py`, 구현 `ts/boundary.py` `adjacent_pairs` (94eeb4e)
- [x] T008 [R-6] 공통 관측: 겹치는 창 안, 시각 차가 허용 오차 이하, 가장 가까운 박스와 일대일, 허용 오차 경계값 포함 — 테스트 `t/test_stitcher_boundary.py`, 구현 `ts/boundary.py` `common_observations` (94eeb4e)
- [x] T009 [R-4] 쌍 연결: 앞 `touchesTail` × 뒤 `touchesHead`, 같은 클래스만, 평균 IoU·최소 공통 관측 경계값, IoU 내림차순 일대일, 같은 점수의 결정적 순서. 클래스가 다르면 잇지 않음(US-1 AC2) — 테스트 `t/test_stitcher_boundary.py`, 구현 `ts/boundary.py` `link_pair` (94eeb4e)
- [x] T010 [P] [R-14] ID: 최종 trackId = jobId와 가장 이른 구성원(시작 시각, chunkId, trackLocalId 순), eventId 두 종류 — 테스트 `t/test_stitcher_ids.py`, 구현 `ts/ids.py` (94eeb4e)
- [x] T011 [R-1] `chunk.boundary` 기록: 트랙 파일을 읽어 Job의 청크 목록에 소유·처리 범위·실행 ID와 함께 기록. 상태 저장소 Protocol과 메모리 구현 — 테스트 `t/test_stitcher_service.py`, 구현 `ts/state.py`, `ts/files.py`, `ts/service.py` (cb08a39)
- [x] T012 [R-2] 같은 chunkId를 다시 받으면 먼저 기록한 실행을 유지(같은 실행 ID면 조용히, 다르면 경고 로그) — 테스트 `t/test_stitcher_service.py`, 구현 `ts/state.py`, `ts/service.py` (cb08a39)
- [x] T013 [R-5] 인접 쌍이 모두 기록되면 쌍마다 `track.detected`(origin `stitcher`) 한 번, 이어진 것이 없으면 빈 `stitched`, 결정적 eventId, B를 먼저 받아도 같은 이벤트(계약 스키마 통과) — 테스트 `t/test_stitcher_service.py`, 구현 `ts/service.py` (cb08a39)
- [x] T014 [R-18] 트랙 파일이 없거나 JSON·계약 위반이거나 파일의 ID·범위가 메시지와 다르거나 `tracksKey`가 실행 폴더가 아니면 기록하지 않고 `NonRetryableError` — 테스트 `t/test_stitcher_service.py`, 구현 `ts/files.py` (cb08a39)

## Phase 2: US-2 화면을 떠났다 돌아온 사람 (P2)

- [x] T015 [R-7] `segment.embedded` 기록: 실행별 `vectorsKey`·`embeddingModel`, `chunk.boundary`보다 먼저 와도 기록 — 테스트 `t/test_stitcher_service.py`, 구현 `ts/state.py`, `ts/service.py` (cb08a39)
- [x] T016 [R-9] Re-ID: 경계 연결 뒤 트랙의 평균 크롭 벡터, 같은 클래스·시간 안 겹침·최대 간격 이하·유사도 경계값 이상, 유사도 내림차순, 앞뒤 각각 최대 하나. 유사도 미만이면 따로 남음(US-2 AC2) — 테스트 `t/test_stitcher_reid.py`, 구현 `ts/reid.py` (f59c83a)
- [x] T017 [R-10] 크롭 벡터가 없는 트랙은 Re-ID 후보에서 빠지고 경계 연결 결과만 남음 — 테스트 `t/test_stitcher_reid.py`, 구현 `ts/reid.py` (f59c83a)
- [x] T018 [R-11] Job 안 벡터 파일의 인코더 모델 ID가 둘 이상이면 Re-ID를 하지 않고 `reidSkippedReason = embedding_model_mismatch`, `embeddingModel = null` — 테스트 `t/test_stitcher_finalize.py`, 구현 `ts/finalize.py` (cb08a39)

## Phase 3: US-3 완료 판정과 번역표 (P1)

- [x] T019 [P] [R-8] 커버리지: 소유 범위 합집합이 `[0, sourceDurationMs)`를 빈틈없이 덮는지, 빈 범위 목록 — 테스트 `t/test_stitcher_coverage.py`, 구현 `ts/coverage.py` (f59c83a)
- [x] T020 [R-13] 번역표: 받은 모든 로컬 트랙이 정확히 한 번, 연결마다 종류·점수·(reid면) 간격, 구성원 시간순, 최종 트랙 시간 범위 — 테스트 `t/test_stitcher_mapping.py`(계약 스키마 통과), 구현 `ts/mapping.py` (f59c83a)
- [x] T021 [R-8] 소유 범위가 다 덮이고 기록한 모든 실행의 벡터가 모이면 Re-ID·확정을 한 번 실행(어느 쪽 메시지가 마지막이어도) — 테스트 `t/test_stitcher_service.py`, 구현 `ts/service.py`, `ts/finalize.py` (cb08a39)
- [x] T022 [R-12] 번역표를 `sources/<sourceId>/jobs/<jobId>/tracks/mapping.json`에 저장한 뒤 `track.manifest`(`complete: true`, 결정적 eventId) 발행. 저장 실패면 발행하지 않음. 같은 입력을 섞은 순서로 다시 넣어도 같은 eventId·trackId(US-3 AC2) — 테스트 `t/test_stitcher_finalize.py`, `t/test_stitcher_service.py`, 구현 `ts/finalize.py` (cb08a39)
- [x] T023 [R-17] manifest를 발행한(또는 확정 중인) Job의 `chunk.boundary`·`segment.embedded`는 기록·재발행 없이 경고 로그 — 테스트 `t/test_stitcher_service.py`, 구현 `ts/state.py`, `ts/service.py` (cb08a39)
- [x] T024 [R-21] 단계 블록 `stitcher.boundary`(비교한 트랙 쌍 수), `stitcher.reid`(비교한 트랙 수), `stitcher.finalize`(최종 트랙 수) — 테스트 `t/test_stitcher_service.py`, 구현 `ts/service.py`, `ts/finalize.py` (cb08a39)

## Phase 4: US-4 오지 않는 청크와 잘못된 입력 (P2)

- [x] T025 [R-15] 마지막 입력 뒤 대기 제한 시간이 지나면 주기 확인이 받은 청크만으로 경계 연결·(벡터 있는 트랙의) Re-ID를 마치고 `complete`·`missingRanges`를 담아 발행. 청크가 하나도 없는 Job은 발행하지 않고 경고(plan 8절 6번). 단계 측정을 핸들러 밖에서 남김(8절 7번) — 테스트 `t/test_stitcher_sweeper.py`(가짜 시계), 구현 `ts/sweeper.py` (6d9857c)
- [x] T026 [R-19] `video.deleted`면 그 영상의 모든 Job 대기 상태를 지우고, 그 뒤 그 영상의 입력은 기록하지 않음 — 테스트 `t/test_stitcher_service.py`, 구현 `ts/service.py`, `ts/state.py` (cb08a39)
- [x] T027 [R-20] 저장소·상태 저장소·발행 오류는 잡지 않고 던지고, 다시 처리하면 같은 쌍 이벤트를 다시 냄 — 테스트 `t/test_stitcher_service.py` (cb08a39)
- [x] T028 [R-16] [NFR-2] [NFR-3] Redis 구현: 원자 기록 스크립트(동시 기록에도 인접 쌍을 놓치지 않음), 확정 임대(한 Pod만, 만료 뒤 이어 받기), 키 TTL 7일, 삭제 표시. 서비스 인스턴스 둘이 같은 Redis에 입력을 나눠 넣어도 manifest 한 번·번역표 같음 — 테스트 `t/test_stitcher_redis.py`(integration, Redis testcontainer), 구현 `ts/redis_state.py` (648054f)
- [x] T029 [NFR-4] 로그에 트랙 좌표·벡터·번역표 내용이 없음(caplog로 전체 흐름 확인) — 테스트 `t/test_stitcher_service.py` (cb08a39)
- [x] T030 [NFR-1] 합성 1시간 영상(청크 30, 로컬 트랙 3,000, 크롭 벡터 9,000)의 Re-ID와 확정 30초 안(측정 약 2.4초라 D-6에 따라 numpy를 쓰지 않는다) — 테스트 `t/test_stitcher_scale.py` (648054f)

## Phase 5: 워커와 배포

- [x] T031 기동 조립: 설정, 프로필(R-22로 실패), Redis, 저장소, 세 경로의 워커, 주기 확인 스레드, health — 테스트 `t/test_stitcher_main.py`, 구현 `ts/__main__.py`, `ts/settings.py` (6801bda)
- [x] T032 [R-18] [R-20] 워커 e2e(Kafka·SeaweedFS·Redis testcontainer): 청크 5개와 벡터 5개 → `track.detected` 4건 + `track.manifest` 1건(계약 스키마 통과), 잘못된 메시지·없는 트랙 파일은 DLQ — 테스트 `t/test_stitcher_worker_e2e.py` (8855a93)
- [x] T033 Dockerfile(멀티스테이지, non-root), README, CI `service-images`에 runtime 이미지 빌드, `services/track-stitcher/.env.example` (3e978b2)
- [x] T034 로컬 compose 확인(docs `local/` PR 뒤): 성공 지표 두 가지(이벤트 수·스키마, 섞은 순서의 같은 번역표). 결과를 아래 "측정 기록"에 남긴다 — 완료: 아래 측정 기록

## Phase 9: v5.1 추가(계약 묶음 4.0.0)

- [x] T035 [R-23] 잠금 4.0.0, `ChunkBoundary`·`SegmentEmbedded`·`TrackDetected`의 선택 필드(`jobType`, `trackIdEpoch`), `STREAM`이면 상태 없이 건너뛰고 로그·계수 — 테스트 `libs/contracts/tests/test_event_contracts.py`, `t/test_stitcher_service.py`, 구현 `vlm_contracts/events.py`, `ts/service.py` (b436d53, cb08a39)

## 측정 기록

2026-11-07, 로컬 compose mock 프로필(M1 Pro, Docker 10 CPU·25GB), docs `scripts/e2e/run.sh measure`

| 항목 | 기준 | 결과 |
| --- | --- | --- |
| `chunk.boundary` 5건·`segment.embedded` 5건 입력 | `track.detected` 4건, `track.manifest` 1건, 계약 통과 | 4건·1건, 이벤트 5건과 번역표 파일 계약 통과 |
| 같은 메시지를 순서만 섞어 다시 입력 | 같은 번역표 | 같다(만든 시각 제외 전체 비교) |

- 확정한 job은 다시 받아도 다시 발행하지 않으므로(R-17) 섞은 입력 전에 그 job의 Redis 상태(`ts:{sourceId}:job:<jobId>:*`)를 지워 처음 받는 상태로 맞췄다.

## 완료 조건 (Definition of Done)

- [x] 이 레포가 담당하는 모든 R-*, NFR-*의 테스트 통과
- [x] ruff check, ruff format --check, mypy, `pytest -m "not ffmpeg and not model"`, `scripts/test-in-docker.sh track-stitcher` 통과
- [x] plan.md와 실제 구현이 일치함(8절 결정 반영)
- [ ] docs 레포 spec 상태 갱신 (`Implementing` / `Done`)
