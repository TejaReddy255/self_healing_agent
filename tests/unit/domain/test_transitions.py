from __future__ import annotations

import itertools

import pytest

from backend.domain.incident import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    IncidentStatus,
    can_transition,
)
from backend.domain.jobs import STAGE_TO_INCIDENT_STATUS, RemediationStage

S = IncidentStatus

# Transcribed independently from docs/architecture.md §7.4.
ARCHITECTURE_7_4: dict[IncidentStatus, set[IncidentStatus]] = {
    S.DETECTED: {S.INVESTIGATING, S.REJECTED},
    S.INVESTIGATING: {S.RCA_COMPLETED, S.FAILED},
    S.RCA_COMPLETED: {S.AWAITING_APPROVAL, S.INVESTIGATING, S.REJECTED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.REJECTED},
    S.APPROVED: {S.EXECUTING, S.FAILED},
    S.EXECUTING: {S.VERIFYING, S.FAILED, S.REPLAN_REQUIRED},
    S.VERIFYING: {S.RECOVERY_RUNNING, S.FAILED, S.REPLAN_REQUIRED},
    S.RECOVERY_RUNNING: {S.RESOLVED, S.FAILED, S.REPLAN_REQUIRED},
    S.REPLAN_REQUIRED: {S.AWAITING_APPROVAL, S.INVESTIGATING, S.FAILED},
    S.FAILED: {S.INVESTIGATING},
    S.RESOLVED: set(),
    S.REJECTED: set(),
}

ALL_PAIRS = list(itertools.product(IncidentStatus, repeat=2))


@pytest.mark.spec("S03-AC4")
@pytest.mark.parametrize(("current", "target"), ALL_PAIRS, ids=[f"{a}->{b}" for a, b in ALL_PAIRS])
def test_can_transition_matches_architecture_table(
    current: IncidentStatus, target: IncidentStatus
) -> None:
    assert can_transition(current, target) is (target in ARCHITECTURE_7_4[current])


@pytest.mark.spec("S03-AC4")
def test_table_covers_every_status() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(IncidentStatus) == set(ARCHITECTURE_7_4)


@pytest.mark.spec("S03-AC4")
@pytest.mark.parametrize("current", [S.DETECTED, S.EXECUTING])
def test_shortcuts_to_resolved_are_forbidden(current: IncidentStatus) -> None:
    assert can_transition(current, S.RESOLVED) is False


@pytest.mark.spec("S03-AC4")
def test_resolved_is_reachable_only_after_recovery() -> None:
    sources = {a for a, targets in ALLOWED_TRANSITIONS.items() if S.RESOLVED in targets}
    assert sources == {S.RECOVERY_RUNNING}


@pytest.mark.spec("S03-AC4")
def test_terminal_statuses_have_no_exits_and_table_is_immutable() -> None:
    assert frozenset({S.RESOLVED, S.REJECTED}) == TERMINAL_STATUSES
    with pytest.raises(TypeError):
        ALLOWED_TRANSITIONS[S.DETECTED] = frozenset({S.RESOLVED})  # type: ignore[index]


@pytest.mark.spec("S03-AC7")
def test_every_remediation_stage_maps_to_an_incident_status() -> None:
    assert set(STAGE_TO_INCIDENT_STATUS) == set(RemediationStage)


@pytest.mark.spec("S03-AC7")
def test_stage_mapping_matches_architecture_7_5() -> None:
    R = RemediationStage
    assert dict(STAGE_TO_INCIDENT_STATUS) == {
        R.QUEUED: S.APPROVED,
        R.PRECHECK: S.APPROVED,
        R.EXECUTING: S.EXECUTING,
        R.TECHNICAL_VERIFY: S.VERIFYING,
        R.DQ_VERIFY: S.VERIFYING,
        R.RECOVERY_TRIGGER: S.RECOVERY_RUNNING,
        R.RECOVERY_VERIFY: S.RECOVERY_RUNNING,
        R.COMPLETED: S.RESOLVED,
        R.FAILED: S.FAILED,
        R.REPLAN_REQUIRED: S.REPLAN_REQUIRED,
    }


@pytest.mark.spec("S03-AC7")
def test_stage_progression_is_a_legal_incident_path() -> None:
    # Walking the happy path of job stages never asks the incident for an illegal transition.
    happy_path = [
        RemediationStage.QUEUED,
        RemediationStage.PRECHECK,
        RemediationStage.EXECUTING,
        RemediationStage.TECHNICAL_VERIFY,
        RemediationStage.DQ_VERIFY,
        RemediationStage.RECOVERY_TRIGGER,
        RemediationStage.RECOVERY_VERIFY,
        RemediationStage.COMPLETED,
    ]
    statuses = [STAGE_TO_INCIDENT_STATUS[stage] for stage in happy_path]
    for current, target in itertools.pairwise(statuses):
        assert current == target or can_transition(current, target), (current, target)
