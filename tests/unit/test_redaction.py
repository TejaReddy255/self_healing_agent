from __future__ import annotations

import io
import json
import logging

import pytest

from backend.observability.logging import build_handler
from backend.observability.redaction import REDACTED, redact_text, redact_value

pytestmark = pytest.mark.spec("S01-AC5")


@pytest.mark.parametrize(
    ("raw", "must_not_contain"),
    [
        ("connecting to postgres://app:s3cr3t@db:5432/ops", "s3cr3t"),
        ("dsn=postgresql+psycopg://svc:p%40ss@host/db", "p%40ss"),
        ("mysql://root:hunter2@mysql:3306/sales failed", "hunter2"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig", "eyJhbGciOiJIUzI1NiJ9"),
        ("header Basic dXNlcjpwYXNzd29yZA== sent", "dXNlcjpwYXNzd29yZA=="),
        ("login failed password=Sup3rS3cret! for user", "Sup3rS3cret!"),
        ("config passwd: 'quoted value'", "quoted value"),
        ('api_key="AKIAABCDEF123456"', "AKIAABCDEF123456"),
        ("token=abc.def.ghi&next=/", "abc.def.ghi"),
        ("client_secret: xyz789 rotated", "xyz789"),
        ("API-KEY=k-123-456", "k-123-456"),
        ("secret = topsecretvalue", "topsecretvalue"),
    ],
)
def test_inline_patterns_are_masked(raw: str, must_not_contain: str) -> None:
    redacted = redact_text(raw)
    assert must_not_contain not in redacted
    assert REDACTED in redacted


def test_text_without_secrets_is_unchanged() -> None:
    text = "task load_customers failed after 3 retries on http://airflow:8080/api/v1"
    assert redact_text(text) == text


def test_sensitive_keys_are_masked_recursively() -> None:
    value = {
        "user": "alice",
        "password": "pw",
        "nested": {"API_KEY": "k", "list": [{"db_dsn": "postgres://a:b@h/d"}]},
        "connection_string": "Server=x;Password=y",
    }
    redacted = redact_value(value)
    assert redacted["user"] == "alice"
    assert redacted["password"] == REDACTED
    assert redacted["nested"]["API_KEY"] == REDACTED
    assert redacted["nested"]["list"][0]["db_dsn"] == REDACTED
    assert redacted["connection_string"] == REDACTED


def test_handler_redacts_message_args_and_extras() -> None:
    stream = io.StringIO()
    logger = logging.getLogger("tests.redaction")
    logger.propagate = False
    logger.handlers = [build_handler(stream)]
    logger.setLevel(logging.INFO)

    logger.info(
        "connecting with %s",
        "postgres://u:pw123@h/db",
        extra={"authorization": "Bearer abc", "details": {"token": "t-1", "rows": 5}},
    )

    line = json.loads(stream.getvalue())
    assert "pw123" not in stream.getvalue()
    assert "t-1" not in stream.getvalue()
    assert line["authorization"] == REDACTED
    assert line["details"] == {"token": REDACTED, "rows": 5}
