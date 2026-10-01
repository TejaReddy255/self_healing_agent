"""Audit metadata reloads (S04): every attempt, automatic or by an admin, gets one audit row."""

from __future__ import annotations

import logging

from backend.domain.audit import AuditEvent
from backend.metadata.models import ReloadResult
from backend.metadata.service import ReloadAuditor
from backend.services.audit_service import AuditService

logger = logging.getLogger(__name__)


def reload_audit_event(result: ReloadResult, subject: str, role: str) -> AuditEvent:
    return AuditEvent(
        phase="metadata",
        event_type="METADATA_RELOAD",
        status=result.outcome,
        operation="reload",
        actor_subject=subject,
        actor_role=role,
        details={
            "previous_version": result.previous_version,
            "version": result.version,
            "error_count": len(result.errors),
            "warning_count": len(result.warnings),
        },
    )


def make_reload_auditor(audit_service: AuditService | None) -> ReloadAuditor:
    async def audit(result: ReloadResult, subject: str, role: str) -> None:
        if audit_service is None:
            logger.warning(
                "audit unavailable: metadata reload not recorded (no database configured)",
                extra={"outcome": result.outcome, "actor": subject},
            )
            return
        await audit_service.record(reload_audit_event(result, subject, role))

    return audit
