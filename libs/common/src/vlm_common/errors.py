"""ai 레포 공통 예외."""


class VlmError(Exception):
    """도메인 예외의 기본 클래스.

    하위 클래스는 `code`를 `<DOMAIN>_<REASON>` 형식으로 정한다. HTTP 응답과 로그는 이 값으로
    오류를 구분한다.
    """

    code = "INTERNAL_ERROR"
