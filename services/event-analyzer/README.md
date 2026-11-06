# event-analyzer

`event.candidate.vector`를 받아 후보마다 ai-gateway `/v1/analyze`(`event_judge`)로 구간 프레임 4장을
판정한다. 1차 판정이 severity `high`이거나 판단 불가면 `event_judge_escalate`로 다시 판정하고, 후보마다
`event.analyzed`를 발행한다. 저장소·DB는 없다.

- spec: docs `docs/specs/SPEC-020-event-analyzer-vector/spec.md`
- 계약: docs `service-contracts/asyncapi/event-analyzer.yaml`, `events/event.analyzed.schema.json`,
  `events/_vlm-analysis.schema.json`
- ai-gateway 호출은 `libs/ai-client`(REST)로 한다. ai-gateway 프로필의 `[callers] allowed`에
  `event-analyzer`가 있어야 한다.

## 실행

```bash
uv run --package event-analyzer python -m event_analyzer
```

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `KAFKA_BOOTSTRAP` | (필수) | Kafka 주소 |
| `EVENT_ANALYZER_AI_GATEWAY_URL` | (필수) | ai-gateway 주소(예: `http://ai-gateway:8000`) |
| `EVENT_ANALYZER_CALL_TIMEOUT_S` | `180` | 판정 호출 한 번(재시도 포함)의 제한 시간. 첫 시도 시각 + 이 값을 `X-Request-Deadline`으로 싣는다(SPEC-021 R-8). ai-gateway 모델 제한 시간보다 길게 둔다 |
| `EVENT_ANALYZER_MAX_RETRIES`, `EVENT_ANALYZER_RETRY_BASE_DELAY_S` | `2`, `1.0` | 요청이 닿지 않은 연결 실패의 재시도 횟수와 첫 대기(두 배씩 는다). 502·503·504·응답 시간 초과는 VLM 비용이 두 번 들 수 있어 다시 부르지 않는다(SPEC-021 R-9) |
| `EVENT_ANALYZER_CONCURRENCY` | `4` | 한 메시지 안의 동시 판정 수 |
| `EVENT_ANALYZER_GROUP_ID` | `event-analyzer` | 소비 그룹. 재시도 `event.candidate.vector.<그룹>.retry`, DLQ `<그룹>.dlq` |
| `STAGE_TIMING_PUBLISH` | `false` | 처리 시간을 `stage.timing`에 발행한다(SPEC-013) |
| `HEALTH_PORT`, `LOG_LEVEL` | `8000`, `INFO` | health 서버 포트, 로그 수준 |

- 판정 호출이 끝내 실패하거나(재시도 소진, 5xx, `AI_DEADLINE_EXCEEDED`(504), `AI_IMAGE_NOT_FOUND`, 계약과 맞지 않는 응답) 그 후보는
  `unknown`과 실패 코드로 발행한다. 다른 후보는 그대로 판정한다.
- ai-gateway가 그 밖의 4xx(호출자·경로·taskType 거절)를 돌려주거나 계약과 맞지 않는 메시지는 아무것도
  발행하지 않고 재시도 없이 DLQ로 간다. Kafka 발행 실패는 재시도 토픽으로 간다.

## 테스트

```bash
uv run pytest services/event-analyzer -m "not integration"   # 단위·서비스
uv run pytest services/event-analyzer                        # Kafka e2e 포함(Docker 필요)
scripts/test-in-docker.sh event-analyzer                     # 서비스 이미지 안에서 전체
```
