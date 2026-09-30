from __future__ import annotations

import typing

import pytest
from pydantic import SecretStr, ValidationError

from backend.app import create_app
from backend.config import ConfigurationError, Settings, load_settings

pytestmark = pytest.mark.spec("S01-AC3")


def _is_secret(annotation: object) -> bool:
    return annotation is SecretStr or SecretStr in typing.get_args(annotation)


def test_no_credential_setting_has_a_default_value() -> None:
    secret_fields = {
        name: field for name, field in Settings.model_fields.items() if _is_secret(field.annotation)
    }
    assert secret_fields, "expected at least one credential setting"
    for name, field in secret_fields.items():
        assert field.default is None, f"{name} must not have a default credential"


def test_prod_without_token_source_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://ops.example.com"]')
    with pytest.raises(ConfigurationError, match="API_TOKENS"):
        load_settings()


def test_prod_with_wildcard_cors_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("API_TOKENS", "{}")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["*"]')
    with pytest.raises(ConfigurationError, match="CORS_ALLOW_ORIGINS"):
        load_settings()


def test_create_app_surfaces_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    with pytest.raises(ConfigurationError):
        create_app()


def test_valid_prod_settings_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("API_TOKENS", "{}")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://ops.example.com"]')
    settings = load_settings()
    assert settings.env == "prod"
    assert settings.cors_allow_origins == ["https://ops.example.com"]


def test_missing_env_fails_fast() -> None:
    with pytest.raises(ConfigurationError, match="env"):
        load_settings()


def test_validation_errors_never_echo_input(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "prod")
    monkeypatch.setenv("API_TOKENS", '{"leak-me-if-you-can": {}}')
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["*"]')
    with pytest.raises(ConfigurationError) as caught:
        load_settings()
    assert "leak-me-if-you-can" not in str(caught.value)
    assert caught.value.__cause__ is None  # no chained pydantic error that could echo input


def test_non_json_list_setting_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "dev")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "http://a.example")
    with pytest.raises(ConfigurationError, match="must be JSON"):
        load_settings()


def test_version_comes_from_package_metadata() -> None:
    from importlib.metadata import version

    assert Settings(_env_file=None, env="dev").version == version("self-healing-agents")


def test_dev_allows_no_tokens() -> None:
    settings = Settings(_env_file=None, env="dev")
    assert settings.api_tokens is None


def test_unknown_env_value_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, env="staging")  # type: ignore[arg-type]


def test_unreadable_token_file_is_configuration_error(tmp_path: object) -> None:
    from pathlib import Path

    missing = Path(str(tmp_path)) / "missing.json"
    with pytest.raises(ConfigurationError, match="API_TOKENS_FILE"):
        create_app(Settings(_env_file=None, env="test", api_tokens_file=missing))
