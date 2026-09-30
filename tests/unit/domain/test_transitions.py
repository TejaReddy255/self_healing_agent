from __future__ import annotations

import itertools

import pytest

from backend.domain.incident import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    IncidentStatus,
    can_transition,
)
from backend.domain.jobs import STAGE_TO_INCIDENT_STATUS, RemediationStage, incident_status_for

S = IncidentStatus

# Transcribed independently from docs/architecture.md §7.4.
ARCHITECTURE_7_4: dict[IncidentStatus, set[IncidentStatus]] = {
    S.DETECTED: {S.INVESTIGATING, S.REJECTED},
    S.INVESTIGATING: {S.RCA_COMPLETED, S.FAILED},
    S.RCA_COMPLETED: {S.AWAITING_APPROVAL, S.INVESTIGATING, S.REJECTED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.REJECTED},
    S.APPROVED: {S.EXECUTING, S.FAILED, S.REPLAN_REQUIRED},  # amended 2026-09-30 (S03)
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


R = RemediationStage
HAPPY_PATH = [
    R.QUEUED, R.PRECHECK, R.EXECUTING, R.TECHNICAL_VERIFY, R.DQ_VERIFY, R.RECOVERY_TRIGGER,
    R.RECOVERY_VERIFY, R.COMPLETED,
]  # fmt: skip
# Architecture §7.3: the happy path, "any stage -> FAILED | REPLAN_REQUIRED".
STAGE_EDGES = [
    *itertools.pairwise(HAPPY_PATH),
    *((stage, end) for stage in HAPPY_PATH[:-1] for end in (R.FAILED, R.REPLAN_REQUIRED)),
]


@pytest.mark.spec("S03-AC7")
@pytest.mark.parametrize(
    ("current", "target"), STAGE_EDGES, ids=[f"{a}->{b}" for a, b in STAGE_EDGES]
)
def test_every_stage_edge_is_a_legal_incident_transition(
    current: RemediationStage, target: RemediationStage
) -> None:
    before, after = incident_status_for(current), incident_status_for(target)
    assert before == after or can_transition(before, after), (before, after)


@pytest.mark.spec("S03-AC7")
def test_skipped_recovery_waits_for_operator_confirmation() -> None:
    # retry_airflow_after_success=false: DQ_VERIFY -> COMPLETED with no recovery run.
    before = incident_status_for(R.DQ_VERIFY)
    after = incident_status_for(R.COMPLETED, recovery_skipped=True)
    assert (before, after) == (S.VERIFYING, S.RECOVERY_RUNNING)
    assert can_transition(before, after)
    assert not can_transition(before, S.RESOLVED)
    # Only the recorded operator confirmation takes it from here to RESOLVED.
    assert can_transition(after, S.RESOLVED)
    assert incident_status_for(R.COMPLETED) is S.RESOLVED
