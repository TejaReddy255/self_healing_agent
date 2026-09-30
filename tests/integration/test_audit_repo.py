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


async def _audit_rows(database: Database) -> int:
    async with database.connection() as conn:
        return int(await conn.fetchval("SELECT count(*) FROM operation_audit"))


async def test_nan_infinity_and_nul_are_persisted(migrated_db: DatabaseSettings) -> None:
    database = Database(migrated_db)
    await database.open()
    try:
        audit_id = await AuditService(database, max_payload_bytes=4096).record(
            AuditEvent(
                phase="dq",
                event_type="dq_result",
                status="OK\x00",
                output={"mean": float("nan"), "max": float("inf")},
                details={"log": "before\x00after"},
            )
        )
        async with database.connection() as conn:
            row = await conn.fetchrow("SELECT * FROM operation_audit WHERE id = $1", audit_id)
    finally:
        await database.close()
    assert row is not None
    assert row["status"] == "OK"
    assert row["output"] == {"mean": "nan", "max": "inf"}
    assert row["details"] == {"log": "beforeafter"}


async def test_record_joins_the_callers_transaction(migrated_db: DatabaseSettings) -> None:
    database = Database(migrated_db)
    service = AuditService(database, max_payload_bytes=4096)
    event = AuditEvent(phase="review", event_type="approve", status="OK")
    await database.open()
    try:
        with pytest.raises(RuntimeError, match="state change failed"):
            async with database.transaction() as conn:
                await service.record(event, conn=conn)
                raise RuntimeError("state change failed")
        assert await _audit_rows(database) == 0, "audit row must roll back with the change"

        async with database.transaction() as conn:
            await service.record(event, conn=conn)
        assert await _audit_rows(database) == 1
    finally:
        await database.close()


async def test_operation_audit_is_append_only(migrated_db: DatabaseSettings) -> None:
    import asyncpg

    database = Database(migrated_db)
    await database.open()
    try:
        audit_id = await AuditService(database, max_payload_bytes=4096).record(
            AuditEvent(phase="p", event_type="e", status="ok")
        )
        async with database.connection() as conn:
            for statement in (
                "UPDATE operation_audit SET status = 'tampered'",
                "DELETE FROM operation_audit",
                "TRUNCATE operation_audit",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await conn.execute(statement)

            # recorded_at is always server time, even when an INSERT supplies an old value.
            stamped = await conn.fetchval(
                "INSERT INTO operation_audit (phase, event_type, status, recorded_at) "
                "VALUES ('p', 'e', 'ok', now() - interval '2 years') "
                "RETURNING recorded_at > now() - interval '1 minute'"
            )
            assert stamped is True

            # Rows past the 13-month retention may be deleted (set up with the stamp trigger off).
            await conn.execute(
                "ALTER TABLE operation_audit DISABLE TRIGGER operation_audit_stamp_recorded_at"
            )
            old_id = await conn.fetchval(
                "INSERT INTO operation_audit (phase, event_type, status, recorded_at) "
                "VALUES ('p', 'e', 'ok', now() - interval '14 months') RETURNING id"
            )
            await conn.execute(
                "ALTER TABLE operation_audit ENABLE TRIGGER operation_audit_stamp_recorded_at"
            )
            assert (
                await conn.execute("DELETE FROM operation_audit WHERE id = $1", old_id)
                == "DELETE 1"
            )
            assert (
                await conn.fetchval("SELECT count(*) FROM operation_audit WHERE id = $1", audit_id)
                == 1
            )
    finally:
        await database.close()
