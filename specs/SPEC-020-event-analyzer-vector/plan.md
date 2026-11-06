---
spec: SPEC-020
repo: ai
status: Approved
owner: <이름>
created: 2026-11-06
updated: 2026-11-07
---

# Plan: SPEC-020 event-analyzer 벡터 후보 판정 — ai

- spec: docs 레포 `docs/specs/SPEC-020-event-analyzer-vector/spec.md`
- 계약: `events/event.analyzed`·`_vlm-analysis`·`event.candidate.vector`, `stages/v1`, `asyncapi/event-analyzer.yaml`, `openapi/ai-gateway.yaml` 1.0.0 (계약 묶음 3.0.0, docs ADR-0014)

- v5.1(계약 묶음 4.0.0, 2026-11-07, ADR-0016): `event.analyzed`에 선택 필드 `unverifiedReason`(`budget_exceeded`·`model_unavailable`·`call_cap_reached`)·`jobType`, `openapi/ai-gateway.yaml` 1.6.0 응답에 `unverifiedReason`이 더해졌다. R-21(사유 전달)·R-22(`jobType` 전달)를 아래 설계 절대로 더한다. `call_cap_reached`는 탐지기 경로(SPEC-023 R-19)만 만든다.

## 1. 요약

새 Kafka 워커 `services/event-analyzer`를 만든다. `event.candidate.vector`를 받아 후보마다 ai-gateway `/v1/analyze`(`event_judge`)를 부르고, severity `high`이거나 판단 불가면 `event_judge_escalate`로 다시 판정한 뒤 후보마다 `event.analyzed`를 발행한다. DB·저장소는 없다.

ai-gateway 호출은 `libs/ai-client`(지금은 빈 자리)에 REST 클라이언트로 만든다. httpx는 이미 lock에 있는 패키지라 새 의존성이 아니다(내부 동기 호출은 ADR-0002대로 REST, gRPC는 쓰지 않기로 함). 일시 오류 재시도는 클라이언트가 한다.

ai-gateway에는 `event_judge_escalate` 폴더와 프로필 항목을 더하고, 두 taskType의 출력 스키마를 계약 `events/_vlm-analysis.schema.json` 참조로 바꾼다. Registry에 계약의 `events/_vlm-analysis.schema.json` 한 파일을 함께 넣는다(테스트 배치·Dockerfile·묶음).

## 2. 담당 요구사항

경로의 `ea/`는 `services/event-analyzer/src/event_analyzer/`, `t/`는 `services/event-analyzer/tests/`다.

| 요구사항 | 담당 모듈 / 컴포넌트 | 검증 테스트 |
| --- | --- | --- |
| R-1, R-2 | `ea/service.py` `_request` | `t/test_analyzer_service.py` |
| R-3, R-4 | `ea/judgment.py` `needs_escalation` | `t/test_judgment.py`, `t/test_analyzer_service.py` |
| R-5, R-6, R-7 | `ea/judgment.py` `decide` | `t/test_judgment.py` |
| R-8, R-9, R-10 | `ea/service.py`, `ea/ids.py`, `vlm_contracts.events.EventAnalyzed` | `t/test_analyzer_service.py`(계약 스키마 통과), `libs/contracts/tests` |
| R-11 | `vlm_ai_client.client.AiGatewayClient` | `libs/ai-client/tests/test_ai_gateway_client.py` |
| R-12, R-13, R-14 | `ea/service.py`(`AiGatewayError` → 실패 기록) | `t/test_analyzer_service.py` |
| R-15 | `ea/service.py`(`NonRetryableError`) | `t/test_analyzer_service.py`, `t/test_analyzer_worker_e2e.py` |
| R-16 | `vlm_contracts.events.EventCandidateVector` 검증 → 워커가 DLQ | `t/test_analyzer_worker_e2e.py` |
| R-17 | 워커 재시도(SPEC-013 공통). 발행 오류를 그대로 던짐 | `t/test_analyzer_service.py` |
| R-18 | `ea/service.py`(`asyncio.Semaphore`) | `t/test_analyzer_service.py` |
| R-19 | `ea/service.py` 단계 블록 | `t/test_analyzer_service.py` |
| R-20 | `registry/prompts/event_judge_escalate/`, 프로필 `[tasks.event_judge_escalate]` | `services/ai-gateway/tests/test_prompts.py`, `test_analyze_api.py` |
| R-21 | `ea/judgment.py`(`Outcome.unverified_reason`, `decide`), `ea/service.py`, `vlm_ai_client` 생성 모델(`AnalyzeResponse.unverifiedReason`) | `t/test_judgment.py::test_spec020_r21_*`, `t/test_analyzer_service.py::test_spec020_r21_*`, `libs/ai-client/tests`(생성 모델 대조) |
| R-22 | `ea/service.py`(`jobType` 그대로 전달), `vlm_contracts.events.EventAnalyzed`·`EventCandidateVector` | `t/test_analyzer_service.py::test_spec020_r22_*`, `libs/contracts/tests/test_event_contracts.py` |
| NFR-1 | `KafkaWorker`(하나씩 처리, 발행 뒤 커밋) | `t/test_analyzer_worker_e2e.py` |
| NFR-2 | `KafkaWorker`가 핸들러를 기다리며 poll을 이어 감(`libs/messaging`) | `t/test_analyzer_worker_e2e.py`(판정이 `max.poll.interval.ms`보다 오래 걸려도 커밋) |
| NFR-3 | 후보 판정 동시 실행 | 로컬 compose 측정(PR에 기록) |

## 3. 원칙 점검

| 원칙 | 준수 | 비고 |
| --- | --- | --- |
| I 스펙 우선 | ☑ | SPEC-020 Approved (docs #109) |
| II 계약 우선 | ☑ | docs #109 머지, 계약 묶음 3.0.0 |
| III 테스트 우선 | ☑ | R-1~R-20, NFR-1·NFR-2 테스트 매핑, NFR-3은 로컬 측정 |
| IV 서비스 자율성 | ☑ | 저장소·DB 없음. 이미지는 ai-gateway가 읽는다 |
| V 단순성 | ☑ | 새 패키지 없음(httpx는 lock에 있음). 설정은 환경변수만, 프로필 파일 없음 |
| VI 관측 가능성 | ☑ | 단계 `analyzer.judge`·`analyzer.escalate`, 결과에 호출 ID·모델·프롬프트 버전 |
| VII 보안 | ☑ | 근거 프레임 경로만 넘기고 이미지·판정 문장을 로그에 남기지 않음 |
| VIII 문서·주석 | ☑ | 계약 모델 테스트, README |
| IX GitOps | ☑ | 운영 배포는 사용자 지시 시 |

## 4. 기술 설계

### 패키지

| 경로 | 내용 |
| --- | --- |
| `libs/contracts/.../events.py` | `VLMAnalysis`, `VectorEvidence`, `Judgment`, `EventAnalyzed`, `EventType`·`Severity` |
| `libs/contracts/.../stages.py` | 단계 ID 4개 |
| `libs/ai-client/src/vlm_ai_client/` | `AiGatewayClient.analyze`, `AnalyzeRequest`·`AnalyzeResponse`, `AiGatewayError`, `CallRetry` |
| `ea/settings.py` | 환경변수 `EVENT_ANALYZER_`: 소비 그룹, ai-gateway 주소, 호출 제한 시간, 재시도 횟수·대기, 동시 판정 수 |
| `ea/judgment.py` | 판정 결과 하나(`Outcome`), 재판정 조건, 최종 결과·판정 방식·검토 표시 |
| `ea/ids.py` | eventId = uuid5(`vlm:event.analyzed:<candidateId>`) |
| `ea/service.py` | 핸들러 |
| `ea/__main__.py` | 조립·기동 |

### ai-client

- `POST /v1/analyze`, 헤더 `X-Caller`(서비스 이름)·`traceparent`. 응답은 계약 모델로 검증한다.
- 오류는 `AiGatewayError(code, status)` 하나로 알린다. `code`는 ProblemDetail의 `code`, 없으면 `AI_HTTP_<status>`. 제한 시간 초과는 `AI_TIMEOUT`, 연결 실패는 `AI_CONNECTION_FAILED`, 계약과 맞지 않는 응답은 `AI_INVALID_RESPONSE`.
- 502·503·시간 초과·연결 실패만 `retryable`이고, `max_retries`번까지 지수 대기(`base_delay_s × 2^(n-1)`) 후 다시 부른다(R-11). 대기 함수는 테스트가 바꿔 넣는다.
- 테스트는 httpx 내장 `MockTransport`로 한다(respx를 더하지 않는다).

### 처리 순서

1. 워커가 `EventCandidateVector`로 검증한다. 맞지 않으면 DLQ(R-16).
2. `analyzer.judge`: 후보마다 `event_judge`를 동시 판정 수 상한 안에서 부른다. 변수는 `eventType`, `startSec`·`endSec`(ms/1000), `zoneName` null, `evidence`(path `vector`, score, threshold, prototypeVersion, embeddingModel).
3. `analyzer.escalate`: 1차가 ai-gateway 결과이고 `unverified`가 아니며 severity `high`이거나 `eventDetected` null인 후보만 `event_judge_escalate`로 다시 부른다. 대상이 없으면 블록을 남기지 않는다.
4. 후보마다 `decide`로 최종 분석·방식·검토 표시를 정하고 `event.analyzed`를 발행한다. 키는 sourceId(AsyncAPI).

- 호출 실패 처리: `AI_IMAGE_NOT_FOUND`와 재시도 가능 오류(재시도 소진)는 그 호출을 실패로 기록한다(R-12~R-14). 그 밖의 4xx는 `NonRetryableError`로 던져 메시지 전체를 DLQ로 보낸다(R-15). 발행 전에 모든 판정을 끝내므로 DLQ로 갈 때 발행된 것이 없다.
- 1차 실패의 최종 분석은 후보 유형과 `unknown`, 나머지 null이다(ai-gateway `fallback.json`과 같은 모양).
- `AI_INVALID_RESPONSE`는 spec에 따로 없어 R-12·R-13의 "호출 실패"로 기록한다. 후보를 버리지 않는다는 spec 목적에 맞춘 선택이다.
- 발행은 블로킹이라 `asyncio.to_thread`로 넘긴다. 발행 오류는 그대로 던져 재시도 토픽으로 간다(R-17). 일부만 발행됐어도 다시 처리할 때 같은 eventId라 소비자가 흡수한다.

### ai-gateway

- `registry/prompts/event_judge*/output.schema.json`: `$ref: ../../events/_vlm-analysis.schema.json`과 출력 예시만 둔다. 예시는 mock Adapter 응답이다.
- `event_judge_escalate/`: 입력 스키마·`fallback.json`은 `event_judge`와 같은 내용(`$id`만 다름). `fallback.json`이 입력 스키마의 `properties`를 직접 확인하므로 참조로 줄이지 않는다. `prompt.md`는 `event_judge`와 같은 지시로 시작하고 문장은 AI 담당자가 고친다.
- `load_schemas` 기본 폴더와 묶음 폴더에 `events`를 더한다. 이미지·테스트는 계약에서 `events/_vlm-analysis.schema.json` 한 파일만 가져온다.
- 프로필: `local.toml`은 로컬 Ollama의 같은 모델, `local-mock.toml`은 mock.

## 5. 대안과 선택 이유

| 대안 | 장점 | 단점 | 선택 |
| --- | --- | --- | --- |
| 재시도를 ai-client에 둠 | 다른 워커도 같은 규칙을 씀 | 클라이언트 설정이 늘어남 | ☑ |
| 재시도를 event-analyzer에 둠 | 서비스 안에서 보임 | 워커마다 같은 코드 | |
| 1차 판정 전부 → 재판정 전부 순서로 나눔 | 단계 측정 두 개가 spec대로 나뉨 | 재판정이 1차 판정 전체를 기다림 | ☑ |
| 후보마다 1차·재판정을 이어서 | 끝나는 시간이 조금 빠름 | 단계 블록이 겹쳐 `analyzer.escalate`의 count·시간 뜻이 흐려짐 | |
| 계약 `events` 폴더 전체를 Registry에 넣음 | 복사 규칙이 단순 | 프롬프트와 관계없는 이벤트 스키마가 섞임 | |

## 6. 테스트 전략

- 단위: 판정 규칙(`decide`, `needs_escalation`), ID
- 클라이언트: `MockTransport`로 성공·ProblemDetail·재시도·시간 초과·연결 실패·잘못된 응답
- 서비스: 가짜 클라이언트·기록 발행기로 핸들러 전체, 결과를 계약 스키마로 검증
- 계약: 새 모델을 docs 원본과 비교(필드·필수·enum·예시 왕복), 단계 ID
- ai-gateway: `event_judge_escalate` 로드·입출력 동일·API 응답의 모델·프롬프트 버전
- 워커 e2e: Kafka testcontainer와 `MockTransport` 클라이언트로 정상 발행, 잘못된 메시지 DLQ, 403 DLQ, 긴 판정 중 커밋
- 테스트 파일 이름은 다른 서비스와 겹치지 않게 `test_analyzer_*`, `test_ai_gateway_client.py`로 짓는다.
- 로컬 compose: mock 프로필 ai-gateway와 후보 12개 메시지로 처리 시간(NFR-3)

## 7. 리스크와 롤백

- 실제 이미지가 있는 `event.candidate.vector`는 frame-encoder가 로컬에서 만든 것으로만 확인한다. chunk-processor가 아직 `segment.ready`를 내지 않는다.
- ai-gateway 출력 스키마 이동으로 `event_judge` 프롬프트 버전 ID가 바뀐다. 결과 캐시 키가 버전을 포함하므로 이전 캐시를 쓰지 않을 뿐 동작은 같다.
- 롤백: 새 서비스라 끄면 된다. ai-gateway 변경은 기존 테스트로 확인한다.

## 8. 배포 고려사항

- 운영 배포·토픽은 사용자 지시 시 infra·gitops에서 한다.
- 로컬 compose: docs `local/docker-compose.yml`에 event-analyzer와 토픽 `event.analyzed`, `event.candidate.vector.event-analyzer.retry`, `event-analyzer.dlq`를 더한다.

## 9. v5.1 추가 설계(계약 묶음 4.0.0, 2026-11-07)

- ai-client의 요청·응답 모델은 SPEC-021대로 `openapi/ai-gateway.yaml`에서 생성하므로 잠금 4.0.0으로 다시 생성하면 `AnalyzeResponse.unverifiedReason`이 들어온다. 손으로 쓴 모델은 두지 않는다.
- `decide`는 최종 결과가 `unverified`일 때 그 결과를 만든 응답(1차 판정 또는 재판정)의 `unverifiedReason`을 `Outcome.unverified_reason`으로 옮기고, 핸들러가 `EventAnalyzed.unverified_reason`에 싣는다. 응답에 사유가 없거나 최종 결과가 `unverified`가 아니면 싣지 않는다. 호출 실패(`unknown`, R-12~R-14)에는 사유가 없다.
- `jobType`은 `EventCandidateVector.job_type`을 `EventAnalyzed.job_type`에 그대로 옮긴다. 없으면 싣지 않는다. 벡터 경로는 값을 읽어 동작을 바꾸지 않는다(Redis 재판정 키는 탐지기 경로 SPEC-023 R-18만 쓴다).
