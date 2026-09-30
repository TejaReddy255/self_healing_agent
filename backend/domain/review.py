"""Review, approval payload and approve request (src §9.2, architecture §7.1, ADR-006)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import PositiveInt, StringConstraints, model_validator

from backend.domain.common import Contract, NonEmptyStr, Sha256Hex
from backend.domain.remediation import RemediationPlan, check_approvable
from backend.domain.sop import SOPMatchStatus, SOPProposal, SOPReference, check_sop_match


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    REJECTED = "REJECTED"


class ApprovalPayload(Contract):
    """Exactly what a reviewer approves. Its canonical hash is the approval boundary.

    ``schema_version`` is hashed with the payload, so a later contract version is an explicit,
    dispatchable change: execution refuses a version it does not know (ADR-006).
    """

    schema_version: Literal[1] = 1
    sop_match_status: SOPMatchStatus
    matched_sop_reference: SOPReference | None
    proposed_sop: SOPProposal | None
    proposed_remediation: RemediationPlan | None

    @model_validator(mode="after")
    def _check_approvable(self) -> Self:
        check_sop_match(self.sop_match_status, self.matched_sop_reference)
        if self.proposed_remediation is not None:
            check_approvable(self.proposed_remediation)
        return self


class ReviewRevisionResult(Contract):
    """Output of the Review Revision (W2) and Execution Replan (W5) agents."""

    decision: Literal["ACCEPT", "REJECT"]
    validated_payload: ApprovalPayload | None = None
    summary: NonEmptyStr
    notes: tuple[NonEmptyStr, ...] = ()
    violations: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _accept_carries_payload(self) -> Self:
        if self.decision == "ACCEPT" and self.validated_payload is None:
            raise ValueError("an ACCEPT decision requires validated_payload")
        return self


class ApproveRequest(Contract):
    """Body of ``POST /reviews/{id}/approve``: CAS on what the reviewer saw (ADR-006)."""

    expected_revision: PositiveInt
    expected_payload_hash: Sha256Hex
    comment: Annotated[str, StringConstraints(max_length=2000)] | None = None
