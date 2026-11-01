---
paths:
  - "pyproject.toml"
---

# lint 설정 (ai)

검사 예외(무시 주석)의 형식은 루트 AGENTS.md의 "검사 예외 (무시 주석)"를 따른다.

설정은 모두 `pyproject.toml`에 둔다.

| 도구 | 설정 | 검사 내용 |
| --- | --- | --- |
| ruff format | `line-length = 100` | 포맷 |
| ruff check | 아래 `select` | lint, import 정렬 |
| mypy | `strict = true`, `plugins = ["pydantic.mypy"]` | 타입 |

ruff `select`와 그 규칙이 강제하는 이 레포의 규칙은 다음과 같다.

| 규칙 | 강제하는 것 |
| --- | --- |
| `E`, `W`, `F`, `I`, `N`, `B`, `SIM`, `RUF` | 기본 오류, import 정렬, 이름 규칙, 흔한 버그 |
| `UP` | 내장 제네릭(`list[str]`, `X \| None`) 사용 |
| `T20` | `print` 금지 |
| `BLE`, `S110` | `except Exception` 남용, 예외 삼키기 금지 |
| `DTZ` | timezone 없는 `datetime` 금지 |
| `PTH` | `os.path` 대신 `pathlib` |
| `TID251` (banned-api) | `os.environ`, `os.getenv` 직접 사용 금지 (`Settings`를 쓴다) |
| `S` | 보안 (하드코딩된 비밀값, 안전하지 않은 호출) |
| `ASYNC` | async 함수 안의 블로킹 호출 |
| `PT` | pytest 작성 규칙 |
| `FAST` | FastAPI 작성 규칙 |

## 설정 단위 예외 (이 목록만 허용)
- `tests/**`: `S101`(assert)을 `per-file-ignores`로 허용한다(pytest는 assert로 검증한다).
- `notebooks/`: ruff와 mypy 대상에서 제외한다(실험용이며 `src`에서 import하지 않는다).
