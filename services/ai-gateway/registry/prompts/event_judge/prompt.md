# 작업: 이벤트 후보 판정

탐지기와 규칙이 찾은 이벤트 후보가 실제로 일어났는지 판정한다.

입력:

- 이미지: 후보 전후의 근거 프레임. 순서대로 주어지며, 관련 객체에 박스가 그려져 있을 수 있다. 얼굴·번호판은 블러 처리되어 있다.
- 후보 JSON: 후보의 이벤트 유형(eventType), 정규화 영상 기준 시각(startSec, endSec), 구역 이름(zoneName), 탐지 근거(evidence).

판정 방법:

1. 이미지에서 후보 eventType에 해당하는 상황이 보이면 eventDetected를 true, verificationStatus를 "confirmed"로 둔다.
2. 상황이 보이지 않거나 다른 상황이면 eventDetected를 false, verificationStatus를 "rejected"로 둔다. 다른 이벤트 유형이 분명하면 eventType에 그 유형을 쓴다.
3. 이미지가 흐리거나 가려져 판단할 수 없으면 eventDetected를 null, verificationStatus를 "unknown"으로 둔다.
4. description에는 누가, 어디서, 무엇을 했는지 한두 문장으로 쓴다. 신원은 쓰지 않는다.
5. beforeState와 afterState에는 이벤트 전후의 현장 상태를 짧게 쓴다. 알 수 없으면 null로 둔다.
6. 사람에게 위험이 남아 있으면 riskDetected를 true로 두고 riskType과 riskReason을 함께 쓴다. 위험이 없으면 false, 알 수 없으면 null로 둔다.
7. solution에는 현장에서 할 조치를 한 문장으로 제안한다. 확정 지시가 아니라 안전관리자가 확인할 제안이다.
8. severity는 사람이 다쳤거나 다칠 수 있으면 "high", 규정 위반이지만 당장 위험이 크지 않으면 "medium", 그 밖은 "low"로 둔다.
9. evidenceStart와 evidenceEnd에는 상황이 보이는 구간의 시각(초)을 후보 시각 범위 안에서 쓴다. 알 수 없으면 null로 둔다.
