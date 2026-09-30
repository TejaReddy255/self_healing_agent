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
        # Review findings (S01 code + security review): prefixed keys, quoted keys, URL passwords
        # containing '@' or '/', and the remaining spec keys.
        ("PG_PASSWORD=pgpass1", "pgpass1"),
        ("access_token=at-999", "at-999"),
        ("refresh_token: rt-888", "rt-888"),
        ("db_password=dbp777", "dbp777"),
        ("{'password': 'dictpw'}", "dictpw"),
        ('{"token": "jsontok"}', "jsontok"),
        ("postgres://u:p@ss@host:5432/db", "ss@host"),
        ("postgres://u:ab/cd@host/db", "ab/cd"),
        ("authorization=raw-auth-value", "raw-auth-value"),
        ("credential=cred-1", "cred-1"),
        ("dsn: host=db user=x", "host=db"),
        ("Cookie: sessionid=abc123", "abc123"),
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


def _capture(logger_name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(logger_name)
    logger.propagate = False
    logger.handlers = [build_handler(stream)]
    logger.setLevel(logging.INFO)
    return logger, stream


def test_non_primitive_extras_are_rendered_then_redacted() -> None:
    class Endpoint:
        def __str__(self) -> str:
            return "mysql://root:objpw@db/sales"

    logger, stream = _capture("tests.redaction.objects")
    logger.info("connecting", extra={"endpoint": Endpoint(), "error": ValueError("token=exc-tok")})
    assert "objpw" not in stream.getvalue()
    assert "exc-tok" not in stream.getvalue()


def test_malformed_log_call_does_not_raise() -> None:
    logger, stream = _capture("tests.redaction.malformed")
    logger.warning("two %s %s", 1)  # one argument missing: must not raise into the caller
    line = json.loads(stream.getvalue())
    assert "log formatting failed" in line["message"]
