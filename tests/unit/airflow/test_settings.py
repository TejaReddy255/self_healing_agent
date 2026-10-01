from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from backend.airflow.settings import AirflowSettings
from backend.config import ConfigurationError


def settings(**values: Any) -> AirflowSettings:
    return AirflowSettings(_env_file=None, **values)


CREDS = {"airflow_username": "svc", "airflow_password": SecretStr("pw")}


@pytest.mark.spec("S05-AC11")
def test_unset_url_means_not_configured_outside_prod() -> None:
    unset = settings()
    assert not unset.configured
    unset.check_for_env("dev")
    assert settings(airflow_base_url="  ", airflow_password="").configured is False


@pytest.mark.spec("S05-AC11")
@pytest.mark.parametrize(
    "values",
    [
        {},
        {"airflow_base_url": "http://airflow.internal:8080", **CREDS},
    ],
    ids=["unconfigured", "plain-http"],
)
def test_prod_requires_configured_https_airflow(values: dict[str, Any]) -> None:
    with pytest.raises(ConfigurationError, match="AIRFLOW_BASE_URL"):
        settings(**values).check_for_env("prod")


@pytest.mark.spec("S05-AC11")
def test_prod_accepts_https_with_credentials() -> None:
    ok = settings(airflow_base_url="https://airflow.example.com/", **CREDS)
    ok.check_for_env("prod")
    assert ok.api_root == "https://airflow.example.com/api/v1"


@pytest.mark.spec("S05-AC11")
@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@airflow.example.com",
        "https://airflow.example.com/?token=x",
        "https://airflow.example.com/#frag",
        "ftp://airflow.example.com",
        "airflow.example.com",
    ],
)
def test_unsafe_base_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        settings(airflow_base_url=url, **CREDS)


@pytest.mark.spec("S05-AC11")
def test_url_requires_credentials_and_never_echoes_them() -> None:
    with pytest.raises(ValidationError, match="AIRFLOW_USERNAME and AIRFLOW_PASSWORD"):
        settings(airflow_base_url="https://airflow.example.com")
    configured = settings(airflow_base_url="https://airflow.example.com", **CREDS)
    assert "pw" not in configured.describe()
    assert "pw" not in repr(configured.airflow_password)


def test_log_limits_must_be_consistent() -> None:
    with pytest.raises(ValidationError, match="CEILING"):
        settings(airflow_log_max_download_bytes=8192, airflow_log_download_ceiling_bytes=4096,
                 airflow_log_max_chars=1000)  # fmt: skip
    with pytest.raises(ValidationError, match="twice"):
        settings(airflow_log_max_chars=20_000, airflow_log_max_download_bytes=30_000)
