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
