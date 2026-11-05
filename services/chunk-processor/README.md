# chunk-processor 실행·종료 경로 (SPEC-014)

이 서비스는 ADR-0009의 backend 담당이며 AI Gateway API/모델을 구현하지 않는다. 실제 Kafka 요청을 소비하고 자체 PostgreSQL의 executionId를 원자적으로 점유한다. 정규화 객체를 S3에서 읽어 FFmpeg로 배정 processingRange만 추출하고, 실제 첫 프레임 PTS와 요청 시작 시각을 구분해 input.mp4/result.json을 실행별 경로에 저장한다. 현재 추론은 명시적인 test 전용 verification 대역이다. output은 검증 표시이며 모델 예측으로 취급하지 않는다.

## 실행과 저장

자체 DB URL은 `CHUNK_PROCESSOR_DATABASE_URL`에 넣는다(Pipeline DB 사용 금지). 시작 시 체크섬을 확인하는 versioned SQL을 적용한다. RUNNING 점유를 먼저 커밋하며 같은 executionId의 재전달/동시 수신을 재실행하지 않는다. 같은 ID의 변경 입력은 충돌이다. 확정된 성공/실패 결과는 종료 상태와 Outbox를 원자 저장하고 기존 KafkaPublisher로 발행한다. 원 요청의 sourceId 키/traceparent를 잇는다. 최초 occurredAt/producedAt/eventId/payload는 저장 후 바꾸지 않는다. 독립 Outbox 주기가 재시작 뒤 미발행 결과도 복구한다.

## 설정과 확인

기본 `CHUNK_PROCESSOR_ENABLED=false`, `ENVIRONMENT=production`, `ADAPTER=unconfigured`다. 운영 inference adapter가 없어 활성 기동하면 실패한다. 검증할 때만 environment=test, adapter=verification, enabled=true와 worker DB/Kafka/S3 설정을 명시하고 `uv run --package chunk-processor python -m chunk_processor`로 실행한다. verification_outcome의 success/failure/unknown은 테스트 대역의 결과만 제어한다. 자동 compose/운영 활성화와 offset 변경은 하지 않는다.

필요 토픽은 chunk.requested, chunk.requested.chunk-processor-v1.retry, chunk-processor-v1.dlq, chunk.execution.finished다. 그룹 변경 시 공통 retry/DLQ 이름 규칙을 적용한다. `/health`, `/ready`는 기존 공통 worker 서버를 사용한다.

## 종료·장애 경계

startBefore가 지난 미실행 요청은 시작하지 않고 START_DEADLINE_EXCEEDED로 종료한다. subprocess 추출 timeout은 실제 자식을 kill·wait한 경우에만 종료 실패다. 실제 AI timeout/워커 중단/종료 저장 실패는 RUNNING이 남고 종료 이벤트·슬롯 자동 반환을 만들지 않는다. 원장이 남았다는 이유로 임의 재실행하지 않는다. 운영자가 실제 종료를 확인한 후 Pipeline 복구 CLI를 사용해야 한다. 자동 임대 회수·worker 원장 재실행/정리·실제 추론/출력 스키마·전체 Job 완료·downstream 적재는 후속이다.

## 검증

`uv run pytest -m "not ffmpeg and not model"`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `scripts/test-in-docker.sh chunk-processor`를 사용한다. backend의 ChunkLifecycleKafkaTest는 실제 normalize/worker 이미지·Kafka·두 서비스 DB·S3를 연결해 최초 v2 재전달, 성공/실패 반환, Pipeline 저장 실패/offset 및 다음 청크 배정을 확인한다. 모델/프로필은 변경하지 않았다.
