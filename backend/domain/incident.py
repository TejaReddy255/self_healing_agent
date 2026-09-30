"""Incident lifecycle (src §21.3, §24.1, §35.6; architecture §7.4 and §18 C1)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class IncidentStatus(StrEnum):
    DETECTED = "DETECTED"
    INVESTIGATING = "INVESTIGATING"
    RCA_COMPLETED = "RCA_COMPLETED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RECOVERY_RUNNING = "RECOVERY_RUNNING"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class IncidentCategory(StrEnum):
    AIRFLOW_FAILURE = "AIRFLOW_FAILURE"
    DATA_QUALITY = "DATA_QUALITY"
    SLA_BREACH = "SLA_BREACH"
    DEPENDENCY_OUTAGE = "DEPENDENCY_OUTAGE"
    OTHER = "OTHER"


_S = IncidentStatus

# RESOLVED is reachable only from RECOVERY_RUNNING (invariant 7). Terminal statuses have no exits:
# a recurrence opens a new incident linked by ``recurrence_of``.
ALLOWED_TRANSITIONS: Mapping[IncidentStatus, frozenset[IncidentStatus]] = MappingProxyType(
    {
        _S.DETECTED: frozenset({_S.INVESTIGATING, _S.REJECTED}),
        _S.INVESTIGATING: frozenset({_S.RCA_COMPLETED, _S.FAILED}),
        _S.RCA_COMPLETED: frozenset({_S.AWAITING_APPROVAL, _S.INVESTIGATING, _S.REJECTED}),
        _S.AWAITING_APPROVAL: frozenset({_S.APPROVED, _S.REJECTED}),
        # REPLAN_REQUIRED: PRECHECK found the approved plan contradicted (src §25).
        _S.APPROVED: frozenset({_S.EXECUTING, _S.FAILED, _S.REPLAN_REQUIRED}),
        _S.EXECUTING: frozenset({_S.VERIFYING, _S.FAILED, _S.REPLAN_REQUIRED}),
        _S.VERIFYING: frozenset({_S.RECOVERY_RUNNING, _S.FAILED, _S.REPLAN_REQUIRED}),
        _S.RECOVERY_RUNNING: frozenset({_S.RESOLVED, _S.FAILED, _S.REPLAN_REQUIRED}),
        _S.REPLAN_REQUIRED: frozenset({_S.AWAITING_APPROVAL, _S.INVESTIGATING, _S.FAILED}),
        _S.FAILED: frozenset({_S.INVESTIGATING}),
        _S.RESOLVED: frozenset(),
        _S.REJECTED: frozenset(),
    }
)

TERMINAL_STATUSES: frozenset[IncidentStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def can_transition(current: IncidentStatus, target: IncidentStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]
