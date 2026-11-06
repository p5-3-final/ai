---
spec: SPEC-019
repo: ai
status: Approved
owner: <이름>
created: 2026-11-06
updated: 2026-11-06
---

# Plan: SPEC-019 frame-encoder 구간·크롭 벡터와 프로토타입 후보 — ai

- spec: docs 레포 `docs/specs/SPEC-019-frame-encoder/spec.md`
- 계약: `events/segment.ready`·`segment.embedded`·`event.candidate.vector`, `vectors/v1/vector-file`, `stages/v1`, `asyncapi/frame-encoder.yaml` (계약 묶음 2.10.0)

## 1. 요약

새 Kafka 워커 `services/frame-encoder`를 만든다. `segment.ready`를 받아 블러 프레임·크롭을 S3에서 읽고, `libs/encoders`의 인코더(ai-gateway와 같은 구현)로 구간 벡터(4장 정규화 평균)와 크롭 벡터를 만든다. 벡터 파일(JSON)을 실행 폴더에 저장하고 `segment.embedded`를, 프로토타입 점수가 임계값 이상인 후보가 있으면 `event.candidate.vector`를 발행한다. 새 의존성·DB는 없다.

ai-gateway의 인코더 프로필·로드 코드(`EncoderProfile`, `load_encoder`)를 `libs/encoders`로 옮겨 두 서비스가 함께 쓴다. 같은 모델 ID·manifest 검증을 한 곳에서 해야 질의 벡터와 구간 벡터가 갈라지지 않는다(동작이 바뀌지 않는 리팩터링).

## 2. 담당 요구사항

경로의 `fe/`는 `services/frame-encoder/src/frame_encoder/`, `t/`는 `services/frame-encoder/tests/`다.

| 요구사항 | 담당 모듈 / 컴포넌트 | 검증 테스트 |
| --- | --- | --- |
| R-1 | `fe/vectors.py` `segment_vector`, `fe/service.py` | `t/test_vectors.py`, `t/test_encoding_service.py` |
| R-2 | `fe/service.py` | `t/test_encoding_service.py` |
| R-3 | `fe/ids.py` `vectors_key`, `fe/service.py`, `vlm_contracts.vectors.VectorFile` | `t/test_encoding_service.py`(파일을 계약 모델로 읽음), `libs/contracts/tests/test_vector_file_contract.py` |
| R-4 | `fe/service.py` | `t/test_encoding_service.py` |
| R-5 | `fe/ids.py` | `t/test_encoding_service.py`(같은 메시지 두 번 → 같은 eventId·키) |
| R-6 | `fe/service.py` | `t/test_encoding_service.py`(빈 메시지) |
| R-7 | `fe/service.py` | `t/test_encoding_service.py` |
| R-8 | `fe/service.py` | `t/test_encoding_service.py` |
| R-9 | `vlm_encoders.loader.load_encoder`(SPEC-011 R-16과 같은 검증) | `libs/encoders/tests`(기존), `t/test_encoder_main.py` |
| R-10 | `fe/prototypes.py` `Prototypes.build` | `t/test_prototypes.py` |
| R-11 | `fe/prototypes.py` `Prototypes.candidates` | `t/test_prototypes.py` |
| R-12, R-13 | `fe/service.py` | `t/test_encoding_service.py` |
| R-14 | `fe/service.py`, `fe/ids.py` | `t/test_encoding_service.py`(계약 스키마 통과) |
| R-15 | `fe/profile.py` | `t/test_prototype_profile.py` |
| R-16 | `vlm_contracts.events.SegmentReady` 검증 → 워커가 DLQ | `libs/contracts/tests`, `t/test_encoder_worker_e2e.py` |
| R-17 | `fe/service.py`(`NonRetryableError`) | `t/test_encoding_service.py` |
| R-18 | 워커 재시도(SPEC-013 공통) | `t/test_encoding_service.py`(저장소 오류는 그대로 던짐) |
| R-19 | `fe/service.py` 단계 블록 | `t/test_encoding_service.py` |
| NFR-1 | `KafkaWorker`(메시지를 하나씩 처리) | `t/test_encoder_worker_e2e.py` |
| NFR-2 | open_clip 인코더 | 로컬 compose 측정(PR에 기록) |
| NFR-3 | 워커가 핸들러를 별도 스레드에서 기다리며 poll을 이어 감(`libs/messaging`). 핸들러는 인코딩을 `asyncio.to_thread`로 돌린다 | `t/test_encoder_worker_e2e.py`(인코딩이 `max.poll.interval.ms`보다 오래 걸려도 커밋) |

## 3. 원칙 점검

| 원칙 | 준수 | 비고 |
| --- | --- | --- |
| I 스펙 우선 | ☑ | SPEC-019 Approved (docs #106) |
| II 계약 우선 | ☑ | docs #106 머지, 계약 묶음 2.10.0 |
| III 테스트 우선 | ☑ | R-1~R-19, NFR-1·NFR-3 테스트 매핑, NFR-2는 로컬 측정 |
| IV 서비스 자율성 | ☑ | 자기 출력 경로에만 쓴다. DB 없음 |
| V 단순성 | ☑ | 새 의존성 없음. 인코더 로드는 ai-gateway와 공유 |
| VI 관측 가능성 | ☑ | 단계 측정 `encoder.*`, 공통 워커 로그·추적 |
| VII 보안 | ☑ | 입력 경로를 메시지의 실행 폴더 `blurred/`로 제한, 이미지·벡터를 로그에 남기지 않음 |
| VIII 문서·주석 | ☑ | 계약 모델 테스트 |
| IX GitOps | ☑ | 운영 배포는 사용자 지시 시 |

## 4. 기술 설계

### 패키지

| 경로 | 내용 |
| --- | --- |
| `libs/contracts/.../events.py` | `SegmentReady`(경로 검증 포함), `SegmentEmbedded`, `EventCandidateVector` |
| `libs/contracts/.../vectors.py` | `VectorFile` |
| `libs/contracts/.../taxonomy.py` | `DetectableEventType`(분류 체계 v2, 계약 테스트로 일치 확인) |
| `libs/contracts/.../stages.py` | 단계 ID 6개 추가 |
| `libs/encoders/.../loader.py` | `EncoderProfile`, `load_encoder`(ai-gateway에서 옮김) |
| `fe/settings.py` | 환경변수: Kafka, 프로필 경로, 모델 버킷, 캐시 폴더, mock 허용(`FRAME_ENCODER_ALLOW_MOCK`, ai-gateway와 같이 mock 인코더는 따로 켜야 기동한다) |
| `fe/profile.py` | TOML 프로필: `[encoder]`, `[prototypes]`(version, `types.<유형>`의 texts·threshold) |
| `fe/vectors.py` | 정규화·평균·코사인 |
| `fe/prototypes.py` | 프로토타입 벡터와 후보 계산 |
| `fe/ids.py` | 결정적 경로·eventId·후보 ID(uuid5) |
| `fe/service.py` | 핸들러 |
| `fe/__main__.py` | 조립·기동 |

### 처리 순서

1. 워커가 `SegmentReady`로 검증한다. 스키마나 경로 규칙(실행 폴더 `blurred/`)을 어기면 `ValidationError`라 재시도 없이 DLQ로 간다(R-16).
2. `encoder.segments`: 구간마다 프레임 4장을 읽어 인코딩하고 평균한다. 이미지가 없거나 디코딩할 수 없으면 `NonRetryableError`(R-17). 저장소 일시 오류는 그대로 던져 재시도한다(R-18).
3. `encoder.crops`: 크롭을 인코딩한다.
4. `encoder.store`: `VectorFile`을 JSON으로 저장한다. 같은 실행이면 같은 키라 덮어써도 내용이 같다.
5. `segment.embedded` 발행. eventId = uuid5(`vlm:segment.embedded:<executionId>`).
6. `encoder.prototype`: 프로토타입이 있으면 후보를 계산하고, 1개 이상이면 `event.candidate.vector` 발행. eventId = uuid5(`vlm:event.candidate.vector:<executionId>`), 후보 ID = uuid5(`vlm:candidate.vector:<executionId>:<segmentId>:<유형>`).

- 인코딩은 이미지를 한 번에 넣는 묶음(프로필 `batch_size`, 기본 16)으로 나눠 메모리를 묶는다.
- 프로토타입 벡터는 기동 때 한 번 만든다. 유형마다 문장 벡터의 정규화 평균이다.

## 5. 대안과 선택 이유

| 대안 | 장점 | 단점 | 선택 |
| --- | --- | --- | --- |
| 인코더 로드를 `libs/encoders`로 옮겨 공유 | 질의·구간 벡터의 모델 검증이 한 곳 | ai-gateway 코드도 바뀜(리팩터링) | ☑ |
| frame-encoder에 같은 코드를 복사 | ai-gateway를 건드리지 않음 | 검증 규칙이 두 곳에서 갈라질 수 있음 | |
| 프로토타입을 문장마다 비교해 최댓값 | 문장별 표현이 살아남음 | 문장 수에 따라 점수 분포가 달라져 임계값 정하기가 어려움 | |

## 6. 테스트 전략

- 단위: 벡터 계산, 프로토타입, 프로필 검증, ID
- 서비스: 메모리 저장소·가짜 발행기·`MockEncoder`로 핸들러 전체
- 계약: 새 모델 3종·벡터 파일·유형 목록·단계 ID를 docs 원본과 비교
- 워커 e2e: Kafka testcontainer로 정상 처리·잘못된 메시지 DLQ
- 테스트 파일 이름은 `test_encoding_service.py`처럼 다른 서비스와 겹치지 않게 짓는다. 폴더 이름에 하이픈이 있는 서비스는 mypy가 테스트 모듈을 파일 이름만으로 구분해, chunk-processor의 `test_service.py`·ai-gateway의 `test_profile.py`와 겹치면 검사가 멈춘다.
- 실제 모델(`-m model`): open_clip 프로필로 기동하고 구간 벡터 차원·길이 확인
- 로컬 compose: 테스트용 `segment.ready`로 전체 흐름과 처리 시간(NFR-2)

## 7. 리스크와 롤백

- `segment.ready` 생산자(chunk-processor)가 아직 없다. 계약대로 만든 테스트 메시지로만 확인한다.
- 롤백: 새 서비스라 끄면 된다. ai-gateway 리팩터링은 기존 테스트로 확인한다.

## 8. 배포 고려사항

- 운영 배포·토픽·Secret은 사용자 지시 시 infra·gitops에서 한다.
- 로컬 compose: docs `local/docker-compose.yml`에 frame-encoder와 토픽을 더한다.
