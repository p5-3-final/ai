# normalize 결과 재전달

최초 video.normalized는 발행 전에 `sources/<sourceId>/normalizations/<normalizationId>/result-event.json`에 원 입력·정규화 설정과 함께 조건부 생성한다. 재시작·broker 실패 뒤에는 저장된 전체 이벤트(occurredAt·producedAt 포함)를 재사용한다. 같은 ID의 다른 입력/설정은 충돌이다. READY/REJECTED 모두 적용한다. 기존 스냅샷 없는 과거 발행 건의 원 payload는 복원할 수 없어 연결 전 별도 확인이 필요하며 Pipeline 기록/offset을 자동 변경하지 않는다. 모델·정규화 알고리즘 변경은 없다.

producedAt이 없는 이전 스냅샷은 최초 발행 payload를 추정하지 않고 운영 확인을 요구한다. 새 스냅샷은 공통 prepare_event로 최초 시각을 채운 뒤 영속화하고 Publisher는 덮어쓰지 않는다.
