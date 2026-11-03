import pytest
from pydantic import ValidationError
from vlm_common.settings import ServiceSettings


class WorkerSettings(ServiceSettings):
    kafka_bootstrap: str


def test_reads_fields_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("KAFKA_BOOTSTRAP", "kafka:19092")

    settings = WorkerSettings()

    assert settings.log_level == "DEBUG"
    assert settings.kafka_bootstrap == "kafka:19092"


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert ServiceSettings().log_level == "INFO"


def test_unknown_log_level_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError, match="log_level"):
        ServiceSettings()


def test_missing_required_field_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAFKA_BOOTSTRAP", raising=False)

    with pytest.raises(ValidationError, match="kafka_bootstrap"):
        WorkerSettings()
