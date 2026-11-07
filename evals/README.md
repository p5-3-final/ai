# evals

Golden Set 평가 도구다. 배포 단위가 아니다.

- spec: docs `docs/specs/SPEC-030-quality-datasets/spec.md`, plan: `specs/SPEC-030-quality-datasets/plan.md`

## 매칭 규칙 (`evals.matching`, SPEC-030 R-11·R-12)

평가 잡이 예측과 라벨을 같은 규칙으로 맞추는 순수 함수다. 세 종류 모두 기준을 통과한 쌍을 점수 순서로 정렬해 일대일로 짝짓는다. 짝이 없는 라벨은 놓침, 짝이 없는 예측은 오탐이다.

| 함수 | 비교 대상 | 기준(기본값) | 정렬 |
| --- | --- | --- | --- |
| `match_intervals` | 같은 이벤트 유형 | temporal IoU ≥ `min_iou`(0.3) | IoU 내림차순 → 라벨 시작 → 라벨 ID → 예측 ID |
| `match_boxes` | 같은 클래스, 시각 차 ≤ `frame_interval_ms` / 2 | 박스 IoU ≥ `min_iou`(0.5) | IoU 내림차순 → 라벨 시각 → 라벨 ID → 예측 ID |
| `match_state_changes` | 같은 클래스, 대응표 `kinds`로 맞춘 같은 종류 | 시각 차 ≤ `max_delta_ms`(5000) | 시각 차 오름차순 → 라벨 시각 → 라벨 ID → 예측 ID |

- 상태 변화 기본 대응표(`DEFAULT_STATE_CHANGE_KINDS`)는 라벨 `removed`·`moved`·`added`를 결과 `removed`·`moved`·`appeared`에 대응시킨다. 라벨 `restored`는 항상 놓침, 결과 `not_restored`는 항상 오탐이다.
- 재고 변화의 비교 시각은 `state_change_time_ms`로 구한다(`afterMs`, 없으면 `beforeMs`). 둘 다 없으면 비교 대상에서 뺀다.

## 성능 Gate 명령 (`evals.stage_gate`, SPEC-030 R-19)

```bash
uv run --package vlm-evals python -m evals.stage_gate \
  --baseline-sources baseline.txt --candidate-sources candidate.txt \
  [--stage-id analyzer.judge ...] [--max-regression-ratio 0.2] [--min-samples 5]
```

- 목록 파일은 한 줄에 영상 ID(UUID) 하나다. 빈 줄과 `#`로 시작하는 줄은 건너뛴다. 중복은 입력 오류다.
- quality-service `POST /internal/v1/quality/stage-gates`를 부른다. 503과 연결 실패만 2회까지 다시 부른다. 계약에 없는 `X-Request-Deadline` 헤더는 보내지 않는다.
- 종료 코드: Gate 통과 0, Gate 실패(`passed: false`) 1, 호출 실패·계약과 맞지 않는 응답·입력 오류 2.
- 단계별 결과(`stage_id`, p95 두 값, `ratio`, 개수, `status`)와 판정을 JSON 로그로 남긴다.

### 환경변수

레포 `.env.example`에는 권한 설정 때문에 아직 넣지 못했다. 값은 아래 표를 따른다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `EVALS_QUALITY_URL` | (필수) | quality-service 내부 주소(클러스터 안 `http://quality-service:8088`, 로컬 `http://localhost:8088`) |
| `EVALS_CALLER` | `quality-eval` | `X-Caller`. quality-service 허용 호출자 목록에 있어야 한다 |
| `EVALS_TIMEOUT_S` | `30` | 시도 하나의 제한 시간(초) |
| `EVALS_DEADLINE_S` | `120` | 재시도를 포함한 전체 제한 시간(초) |

## 이번 범위에 없는 것

평가 잡 본체(Golden Set 매니페스트 읽기, 예측 모으기, `submitEvaluationRun` 제출)와 벤치마크 영상을 돌리고 이 명령을 부르는 CI workflow는 아직 없다(plan 5절 3).
