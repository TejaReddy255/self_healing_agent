from __future__ import annotations

import uuid

import pytest

from backend.db.pool import Database
from backend.db.settings import DatabaseSettings
from backend.domain.audit import AuditEvent
from backend.observability.redaction import REDACTED
from backend.services.audit_service import TRUNCATION_MARKER, AuditService

pytestmark = [pytest.mark.integration, pytest.mark.spec("S02-AC5")]


async def test_record_persists_correlation_redacts_and_truncates(
    migrated_db: DatabaseSettings,
) -> None:
    ids = {name: uuid.uuid4() for name in ("analysis_id", "review_id", "job_id", "incident_id")}
    event = AuditEvent(
        phase="execution",
        event_type="tool_call",
        status="SUCCESS",
        operation="find_duplicates",
        tool_name="find_duplicates",
        server="postgresql",
        duration_ms=42,
        actor_subject="reviewer@test",
        actor_role="reviewer",
        request_id="req-audit-1",
        pipeline_id="customer_daily_load",
        dag_id="customer_daily_load",
        run_id="manual__2026-09-30",
        task_id="load_customers",
        input={"sql": "SELECT 1", "password": "p4ss", "dsn": "postgres://u:pw@h/db"},
        output={"rows": ["x" * 400 for _ in range(20)]},
        details={"note": "token=tok-123 used"},
        **ids,
    )

    database = Database(migrated_db)
    await database.open()
    try:
        audit_id = await AuditService(database, max_payload_bytes=2048).record(event)
        async with database.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM operation_audit WHERE id = $1", audit_id)
    finally:
        await database.close()

    assert row is not None
    for column in (
        "phase",
        "event_type",
        "status",
        "operation",
        "tool_name",
        "server",
        "duration_ms",
        "actor_subject",
        "actor_role",
        "request_id",
        "pipeline_id",
        "dag_id",
        "run_id",
        "task_id",
    ):
        assert row[column] == getattr(event, column), column
    for name, value in ids.items():
        assert row[name] == value, name
    assert row["occurred_at"] is not None

    assert row["input"] == {"sql": "SELECT 1", "password": REDACTED, "dsn": REDACTED}
    assert row["output"][TRUNCATION_MARKER] is True
    assert row["output"]["original_bytes"] > 2048
    assert "tok-123" not in row["details"]["note"]
