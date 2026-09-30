"""SOP contracts used in analyses and approval payloads (src §9, agent-design §3).

The SOP *catalogue* documents (static YAML and ``approved_sops``) are S04/S10 models.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from backend.domain.common import (
    Contract,
    NonEmptyStr,
    RiskLevel,
    Sha256Hex,
    ShortText,
    SystemName,
)


class SOPMatchStatus(StrEnum):
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    PARTIAL = "PARTIAL"


class SOPReference(Contract):
    """Pins the SOP a plan follows. ``content_sha256`` pins its exact content, so a metadata reload
    between approval and execution cannot change the SOP behind an approved hash (ADR-006)."""

    sop_id: ShortText
    version: ShortText | None = None
    content_sha256: Sha256Hex | None = None


class SOPProposal(Contract):
    """A generated SOP (W3). Installed only when the review containing it is approved (ADR-008)."""

    title: ShortText
    category: ShortText
    target_system: SystemName
    risk_level: RiskLevel
    required_evidence: tuple[NonEmptyStr, ...] = ()
    remediation_steps: tuple[NonEmptyStr, ...] = Field(min_length=1)
    verification_steps: tuple[NonEmptyStr, ...] = ()
    stop_conditions: tuple[NonEmptyStr, ...] = ()
    human_approval_required: Literal[True] = True


def check_sop_match(status: SOPMatchStatus, reference: SOPReference | None) -> None:
    """MATCHED/PARTIAL must name the SOP; NO_MATCH must not."""
    if status is SOPMatchStatus.NO_MATCH and reference is not None:
        raise ValueError("matched_sop_reference must be null when sop_match_status is NO_MATCH")
    if status is not SOPMatchStatus.NO_MATCH and reference is None:
        raise ValueError(f"matched_sop_reference is required when sop_match_status is {status}")
