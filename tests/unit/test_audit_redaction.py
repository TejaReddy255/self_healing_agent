from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from backend.domain.audit import Activity, AuditEvent
from backend.observability.redaction import REDACTED
from backend.services.activity_service import ActivityBroadcaster
from backend.services.audit_service import TRUNCATION_MARKER, prepare_payload


@pytest.mark.spec("S02-AC5")
def test_prepare_payload_redacts_nested_secrets() -> None:
    payload = {
        "sql": "SELECT 1",
        "connection": {"dsn": "postgres://u:pw@h/db", "password": "x"},
        "note": "retry with api_key=abc123",
        "rows": [1, 2, 3],
    }
    result = prepare_payload(payload, 65536)
    assert result is not None
    assert result["sql"] == "SELECT 1"
    assert result["connection"] == {"dsn": REDACTED, "password": REDACTED}
    assert "abc123" not in result["note"]
    assert result["rows"] == [1, 2, 3]


@pytest.mark.spec("S02-AC5")
def test_prepare_payload_truncates_with_marker() -> None:
    big = {"blob": "x" * 5000, "token": "secret-value"}
    result = prepare_payload(big, 1024)
    assert result is not None
    assert result[TRUNCATION_MARKER] is True
    assert result["original_bytes"] > 1024
    assert len(json.dumps(result).encode()) < 1024
    assert "secret-value" not in json.dumps(result)  # redaction happens before truncation


@pytest.mark.spec("S02-AC5")
def test_prepare_payload_normalises_non_json_values() -> None:
    moment = datetime(2026, 9, 30, tzinfo=UTC)
    assert prepare_payload({"at": moment}, 1024) == {"at": str(moment)}
    assert prepare_payload(None, 1024) is None


@pytest.mark.spec("S02-AC5")
def test_prepare_payload_makes_nan_infinity_and_nul_storable() -> None:
    payload = {
        "stats": {"mean": float("nan"), "max": float("inf"), "min": float("-inf"), "n": 3.5},
        "log": "line one\x00line two",
        "key\x00with_nul": [float("nan"), "ok"],
    }
    result = prepare_payload(payload, 65536)
    assert result == {
        "stats": {"mean": "nan", "max": "inf", "min": "-inf", "n": 3.5},
        "log": "line oneline two",
        "keywith_nul": ["nan", "ok"],
    }
    # Serialisable under PostgreSQL's JSON rules: no NaN tokens, no \u0000 escapes.
    encoded = json.dumps(result, allow_nan=False)
    assert "\\u0000" not in encoded


@pytest.mark.spec("S02-AC5")
def test_sanitize_strips_nul_from_text_columns_and_rejects_bad_times() -> None:
    from datetime import timedelta
    from unittest.mock import MagicMock

    from backend.services.audit_service import AuditService

    service = AuditService(MagicMock(), max_payload_bytes=4096)
    event = AuditEvent(phase="exec\x00", event_type="tool_call", status="OK", operation="a\x00b")
    sanitized = service.sanitize(event)
    assert sanitized.phase == "exec"
    assert sanitized.operation == "ab"

    future = AuditEvent(
        phase="p", event_type="e", status="ok", occurred_at=datetime.now(UTC) + timedelta(hours=1)
    )
    with pytest.raises(ValueError, match="future"):
        service.sanitize(future)


def test_audit_event_requires_timezone_aware_times() -> None:
    with pytest.raises(ValueError, match="timezone"):
        AuditEvent(phase="p", event_type="e", status="ok", occurred_at=datetime(2026, 9, 30))


def test_audit_event_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="extra"):
        AuditEvent(phase="p", event_type="e", status="ok", unexpected=1)  # type: ignore[call-arg]


def _activity(activity_id: int) -> Activity:
    return Activity(id=activity_id, created_at=datetime.now(UTC), type="t", details={})


@pytest.mark.spec("S02-AC6")
def test_broadcaster_fans_out_and_unsubscribes() -> None:
    broadcaster = ActivityBroadcaster()
    with broadcaster.subscribe() as first, broadcaster.subscribe() as second:
        broadcaster.publish(_activity(1))
        assert first.get_nowait().id == 1
        assert second.get_nowait().id == 1
    assert broadcaster.subscriber_count == 0
    broadcaster.publish(_activity(2))  # no subscribers: must not fail


@pytest.mark.spec("S02-AC6")
def test_slow_subscriber_drops_oldest_without_blocking() -> None:
    broadcaster = ActivityBroadcaster(queue_size=2)
    with broadcaster.subscribe() as queue:
        for i in range(1, 5):
            broadcaster.publish(_activity(i))
        assert [queue.get_nowait().id for _ in range(queue.qsize())] == [3, 4]
