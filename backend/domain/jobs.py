"""Background job statuses and remediation stages (architecture §7.2, §7.3, §7.5)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from backend.domain.incident import IncidentStatus


class JobStatus(StrEnum):
    """Revision and analysis jobs."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RemediationStage(StrEnum):
    QUEUED = "QUEUED"
    PRECHECK = "PRECHECK"
    EXECUTING = "EXECUTING"
    TECHNICAL_VERIFY = "TECHNICAL_VERIFY"
    DQ_VERIFY = "DQ_VERIFY"
    RECOVERY_TRIGGER = "RECOVERY_TRIGGER"
    RECOVERY_VERIFY = "RECOVERY_VERIFY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"


_R = RemediationStage
_I = IncidentStatus

# The finer verification stages are job stages, not incident statuses (architecture §18 C1).
# Services apply the target status through ``can_transition``; the map never bypasses it.
STAGE_TO_INCIDENT_STATUS: Mapping[RemediationStage, IncidentStatus] = MappingProxyType(
    {
        _R.QUEUED: _I.APPROVED,
        _R.PRECHECK: _I.APPROVED,
        _R.EXECUTING: _I.EXECUTING,
        _R.TECHNICAL_VERIFY: _I.VERIFYING,
        _R.DQ_VERIFY: _I.VERIFYING,
        _R.RECOVERY_TRIGGER: _I.RECOVERY_RUNNING,
        _R.RECOVERY_VERIFY: _I.RECOVERY_RUNNING,
        _R.COMPLETED: _I.RESOLVED,
        _R.FAILED: _I.FAILED,
        _R.REPLAN_REQUIRED: _I.REPLAN_REQUIRED,
    }
)


def incident_status_for(
    stage: RemediationStage, *, recovery_skipped: bool = False
) -> IncidentStatus:
    """Incident status for a job stage (architecture §7.5).

    With ``retry_airflow_after_success = false`` there is no recovery run, so a COMPLETED job leaves
    the incident in RECOVERY_RUNNING until an operator confirms recovery (invariant 7).
    """
    if stage is RemediationStage.COMPLETED and recovery_skipped:
        return IncidentStatus.RECOVERY_RUNNING
    return STAGE_TO_INCIDENT_STATUS[stage]
