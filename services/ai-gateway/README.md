# ai-gateway

외부 AI 호출 관문이다. `/v1/analyze`(VLM 판정), `/v1/embed`(벡터), `/v1/parse`(질의 해석)를 제공한다.

- spec: docs `docs/specs/SPEC-009-ai-gateway-analyze/`, `SPEC-011-ai-gateway-embed/`, `SPEC-012-ai-gateway-parse/`, `SPEC-016-ai-gateway-cache-budget/`, `SPEC-021-internal-call-rules/`
- 계약: docs `service-contracts/openapi/ai-gateway.yaml`
- 실행·마이그레이션 명령은 레포 루트 `AGENTS.md`의 "명령어"에 있다.

## 호출 마감 `X-Request-Deadline` (SPEC-021)

세 operation 모두 선택 헤더 `X-Request-Deadline`(호출자가 기다리는 마지막 시각, epoch ms 정수)을 받는다.

| 경우 | 동작 |
| --- | --- |
| 헤더 없음 | 프로필 제한 시간(`timeout_s`)만 쓴다 |
| 0 이상의 정수가 아님 | 400 `AI_INVALID_REQUEST`, `errors[].field`는 `X-Request-Deadline` |
| 받았을 때 이미 지남 | 504 `AI_DEADLINE_EXCEEDED`. 저장소·결과 캐시·예산·외부 모델을 부르지 않고 호출 로그도 남기지 않는다(JSON 로그 `request rejected`만 남긴다) |
| 남은 시간 있음 | 외부 모델 제한 시간 = min(호출 시점의 남은 시간, 프로필 `timeout_s`). 같은 입력 잠금 대기·분당 한도 대기도 남은 시간까지만 기다린다 |
| 남은 시간 안에 외부 모델이 끝나지 않음 | 504 `AI_DEADLINE_EXCEEDED`. 호출 로그 status `unavailable`, 예산은 공급자 시간 초과와 같이 예약액으로 정산한다. `/v1/parse`도 이때만 `safe-fallback` 대신 504다 |

- 남은 시간 반영은 공급자 Adapter를 부르는 경로(`/v1/analyze`, `embed_text`, parse LLM)에만 한다. 사전 해석과 인코더(`embed_image`, `embed_visual_text`)는 받을 때 지난 마감만 검사한다. 인코더는 스레드의 CPU 작업이라 중간에 멈출 수 없다.
- 마감은 호출자 시계, 비교는 ai-gateway 시계다. 같은 클러스터의 NTP 오차는 무시한다.
- 호출자는 `libs/ai-client`의 `AiGatewayClient`를 쓰면 헤더와 재시도 규칙(analyze는 연결 실패만, embed·parse는 503·연결 실패)이 맞춰진다.
