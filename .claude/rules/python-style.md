---
paths:
  - "src/**/*.py"
  - "tests/**/*.py"
  - "evals/**/*.py"
---

# Python 코드 스타일

- 포맷과 import 정렬은 ruff가 결정한다(`ruff format`, `ruff check --fix`). 줄 길이는 100이다.
- 이름 규칙
  - 모듈, 함수, 변수: `snake_case`
  - 클래스: `PascalCase`
  - 상수: `UPPER_SNAKE_CASE`
  - 비공개: `_prefix`
- 타입 힌트는 내장 제네릭(`list[str]`, `dict[str, int]`, `X | None`)으로 쓴다. `typing.List`나 `Optional`은 쓰지 않는다.
- 데이터 구조
  - 경계(API, 외부 응답)에서는 Pydantic `BaseModel`을 쓴다.
  - 내부 값 객체는 `@dataclass(frozen=True, slots=True)`를 쓴다.
  - dict를 그대로 넘기지 않는다.
- 의존성은 FastAPI `Depends`로 주입한다. 전역 싱글턴을 import해서 쓰지 않는다. 그래야 테스트에서 교체할 수 있다.
- 예외
  - 도메인 예외는 `core/errors.py`의 기본 예외를 상속해서 만든다.
  - `except Exception: pass`는 금지한다. 다시 던질 때는 `raise ... from e`를 쓴다.
- 경로는 `pathlib.Path`, 시간은 timezone-aware `datetime`(`datetime.now(UTC)`)을 쓴다.
- 주석과 docstring은 루트 AGENTS.md의 "주석 규칙"을 따른다.
- 노트북(`notebooks/`)의 코드를 `src`로 옮길 때는 타입, 테스트, 설정 분리까지 같이 한다.
