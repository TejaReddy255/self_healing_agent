"""Shared vocabulary for the v1 domain contracts (S03).

Every contract model is ``frozen`` and rejects unknown fields: inbound API bodies, model outputs and
persisted facts are validated strictly, and a model that cannot change after validation is safe to
hash (``backend.domain.hashing``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, StringConstraints


class Contract(BaseModel):
    """Base class for every v1 contract model."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SystemName(StrEnum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    S3 = "s3"
    MONGODB = "mongodb"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class EvidenceStatus(StrEnum):
    """Evidence labels from the shared evidence policy (src §6.1)."""

    OBSERVED = "OBSERVED"
    VERIFIED = "VERIFIED"
    INFERRED = "INFERRED"
    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    VERIFIED_FIXED = "VERIFIED_FIXED"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# Timezone-aware input only (a naive value would silently take the host's zone), normalised to UTC.
# Pydantic serialises UTC datetimes with a ``Z`` suffix, which the canonical hash relies on.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=256)]

# Airflow-compatible identifiers: letters, digits, "_", "-" and "." (task groups use "group.task").
_IDENTIFIER = r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$"
PipelineId = Annotated[str, StringConstraints(max_length=128, pattern=_IDENTIFIER)]
DagId = Annotated[str, StringConstraints(max_length=250, pattern=_IDENTIFIER)]
TaskId = Annotated[str, StringConstraints(max_length=250, pattern=_IDENTIFIER)]
# Airflow run IDs contain ":" and "+" (e.g. "scheduled__2026-09-30T00:00:00+00:00").
RunId = Annotated[str, StringConstraints(min_length=1, max_length=250, pattern=r"^[^\x00-\x1f]+$")]
RuleId = Annotated[str, StringConstraints(max_length=128, pattern=_IDENTIFIER)]

# Platform entity IDs are UUIDs (architecture §8).
AnalysisId = UUID
ReviewId = UUID
JobId = UUID
IncidentId = UUID
EventId = UUID

# Lower-case hex SHA-256, as produced by ``backend.domain.hashing.payload_hash``.
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
