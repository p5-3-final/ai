---
name: sdd-plan
description: 승인된 spec(SPEC-NNN)을 읽고 이 레포의 specs/SPEC-NNN-*/plan.md와 tasks.md를 작성한다. 기능 구현을 시작하기 전에 사용한다.
argument-hint: SPEC-NNN
---

# /sdd-plan: 구현 계획과 태스크 작성

입력: $ARGUMENTS (Claude Code에서는 인자로 치환된다. 다른 도구에서는 사용자 요청에서 파악한다)

docs 레포의 위치는 이 레포의 상위 두 단계(`../../`)다. 루트 구조로 클론하지 않았다면 사용자에게 docs 레포 경로를 묻는다.

1. **spec 확인**: `../../docs/specs/$ARGUMENTS-*/spec.md`를 읽는다.
   - 상태가 `Approved`가 아니거나 `[NEEDS CLARIFICATION]`이 남아 있으면 **멈추고** 사용자에게 알린다.
   - `repos`에 이 레포가 없으면 멈추고 확인한다.
2. **맥락 수집**: 다음을 읽는다.
   - `../../docs/principles.md`
   - spec이 참조하는 계약(`../../docs/contracts/...`)과 ADR
   - 이 레포의 AGENTS.md와 관련 코드
3. **plan 작성**: `../../docs/templates/plan.md`를 기반으로 `specs/<spec 폴더 이름>/plan.md`를 만든다.
   - 이 레포가 담당하는 요구사항만 다루고, 모든 요구사항을 테스트에 매핑한다.
   - 원칙 점검의 모든 행을 채운다. 위반이 있으면 사유를 적는다.
   - 새 라이브러리, 스키마 변경, 계약 변경, 되돌리기 어려운 선택은 **사용자에게 확인**받는다. ADR이 필요하면 그렇다고 알린다.
4. **tasks 작성**: `../../docs/templates/tasks.md`를 기반으로 같은 폴더에 `tasks.md`를 만든다.
   - 태스크 하나를 요구사항 하나의 TDD 사이클(테스트와 구현)로 잡고, 각 태스크에 `[R-n]`과 테스트·구현 경로를 적는다.
5. **보고**: plan의 핵심 결정, 확인이 필요한 항목, 태스크 수를 요약한다. 다음 단계로 `/sdd-implement $ARGUMENTS`를 안내한다.
