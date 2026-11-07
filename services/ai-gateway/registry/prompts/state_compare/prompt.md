# 작업: 전후 장면 비교

산업 현장 고정 카메라의 두 장면을 비교한다. 첫 번째 이미지는 직전 장면이고 그 장면에 expectedClasses 객체가 있었다. 두 번째 이미지는 이후 장면이다. 이 문안은 자리 표시이며 AI 담당자가 다듬는다(SPEC-028).

입력 JSON:

- eventType: 변화가 확인되면 analysis.eventType에 쓸 유형
- changeKind: 재고 비교가 찾은 후보. removed(사라짐), moved(다른 자리로 옮겨짐), not_restored(작업 전 영상에 있던 방호 시설이 돌아오지 않음)
- expectedClasses: 직전 장면에 있던 객체 클래스
- zoneName: 객체가 있던 구역 이름(없으면 null)
- beforeSec·afterSec: 두 장면의 시각(초). beforeSec가 null이면 직전 장면은 앞 영상이다.

판정 방법:

1. 두 장면에서 expectedClasses 객체가 사라졌는지, 옮겨졌는지, 그대로인지 본다. 보이는 것만 쓴다.
2. 후보와 같은 변화가 보이면 verificationStatus는 confirmed, 변화가 없으면 rejected, 이미지로 알 수 없으면 unknown이다.
3. changes에는 본 변화를 객체마다 한 줄로 쓴다. 방호가 풀려 위험이 생겼으면 riskDetected와 그 이유를 쓴다.
4. 사람의 신원, 이미지에 없는 사실은 쓰지 않는다.
