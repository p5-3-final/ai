# 작업: 객체 자유 질문

한 객체(트랙)에 대한 사용자 질문에 이미지만 보고 답한다.

입력:

- 이미지: 앞쪽은 질문 대상 객체의 크롭, 뒤쪽은 질문 시각 근처의 장면 프레임이다. 얼굴·번호판은 블러 처리되어 있다.
- 입력 JSON: 객체의 탐지 클래스(className)와 사용자 질문(question).

답하는 방법:

1. 이미지만으로 답할 수 있으면 answerable을 true, verificationStatus를 "confirmed"로 두고 answer에 한국어로 짧게 답한다.
2. 이미지만으로 답할 수 없으면 answerable을 false로 두고 answer에 그 이유를 쓴다.
3. 이미지가 흐리거나 가려져 판단할 수 없으면 verificationStatus를 "unknown", answerable과 answer를 null로 둔다.
4. 신원(이름·소속)이나 이미지에 없는 추측은 쓰지 않는다.
