# 작업: 영상 요약

영상 하나의 타임라인과 위험 상태를 읽고 안전관리자가 먼저 읽을 요약을 쓴다. 이 문안은 자리 표시이며 AI 담당자가 다듬는다(SPEC-027).

입력 JSON:

- durationSec: 영상 길이(초)
- zones: 구역 ID·이름·종류
- timeline: 시각(atMs) 순 항목. kind는 event(판정 이벤트), rule_event(체류·배회 규칙), inventory_event(방호벽 이동·제거·미복구, 장비 방치), risk_activated(위험 시작), risk_resolved(위험 해소)다.
- risks: 위험 상태와 노출 지표. carriedOver가 true면 앞 영상에서 이어 온 위험이다.

작성 방법:

1. summary에는 한국어로 두세 문장을 쓴다. 언제 어떤 위험이 생기고 해소됐는지, 영상 끝까지 남은 위험이 있는지를 쓴다.
2. 입력에 없는 사실, 사람의 신원, 추측은 쓰지 않는다. 구역은 이름으로 쓴다.
3. highlights에는 중요한 순간을 최대 5개, 타임라인 항목의 atMs와 한 줄 설명으로 쓴다.
