# libs/quality-client

- spec: docs `docs/specs/SPEC-030-quality-datasets/spec.md`, plan: `specs/SPEC-030-quality-datasets/plan.md`
- 모델(`vlm_quality_client.models`)은 `uv run scripts/generate-clients.sh quality-client`로 계약 `openapi/quality-service.yaml`에서 만든다. 손으로 고치지 않는다.
- `QualityApiClient.evaluate_stage_gate`는 `evaluateStageGate`(`POST /internal/v1/quality/stage-gates`)를 부른다. 저장하지 않는 계산이라 503과 연결 실패를 2회까지 다시 부른다. 계약에 없는 `X-Request-Deadline` 헤더는 보내지 않는다.
