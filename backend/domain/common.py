"""Shared vocabulary for the v1 domain contracts (S03).

Every contract model is ``frozen`` and rejects unknown fields: inbound API bodies, model outputs and
persisted facts are validated strictly. Sequences are tuples, so a validated model's lists cannot be
edited in place. ``frozen`` does not reach inside ``dict`` fields, so approval checks hash the
persisted canonical bytes rather than trusting an in-memory model (ADR-006).
"""

from __future__ import annotations

import math
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    JsonValue,
    StringConstraints,
)


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


def _clean_text(value: str) -> str:
    """NFC-normalise and refuse characters that make text look different from what is stored.

    Control characters (other than tab and newline) and format characters (bidi overrides,
    zero-width spaces) would let a reviewer see one thing while a different string is hashed and
    later executed.
    """
    value = unicodedata.normalize("NFC", value)
    for char in value:
        if char in "\t\n":
            continue
        if unicodedata.category(char) in ("Cc", "Cf", "Cs"):
            raise ValueError(f"text contains a control or invisible character U+{ord(char):04X}")
    return value


def _finite_json(value: JsonValue) -> JsonValue:
    """Reject NaN/Infinity anywhere in a free-form JSON value (they have no canonical form)."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("NaN and Infinity are not allowed")
    if isinstance(value, dict):
        for item in value.values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)
    return value


# Timezone-aware input only (a naive value would silently take the host's zone), normalised to UTC.
# Pydantic serialises UTC datetimes with a ``Z`` suffix, which the canonical hash relies on.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]

NonEmptyStr = Annotated[
    str, StringConstraints(min_length=1, max_length=10_000), AfterValidator(_clean_text)
]
ShortText = Annotated[
    str, StringConstraints(min_length=1, max_length=256), AfterValidator(_clean_text)
]
JsonObject = Annotated[dict[str, JsonValue], AfterValidator(_finite_json)]

# Airflow-compatible identifiers: letters, digits, "_", "-" and "." (task groups use "group.task").
_IDENTIFIER = r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$"
PipelineId = Annotated[str, StringConstraints(max_length=128, pattern=_IDENTIFIER)]
DagId = Annotated[str, StringConstraints(max_length=250, pattern=_IDENTIFIER)]
TaskId = Annotated[str, StringConstraints(max_length=250, pattern=_IDENTIFIER)]
# Airflow run IDs contain ":" and "+" (e.g. "scheduled__2026-09-30T00:00:00+00:00"). Clients must
# URL-encode them (S05).
RunId = Annotated[str, StringConstraints(min_length=1, max_length=250, pattern=r"^[^\x00-\x1f]+$")]
RuleId = Annotated[str, StringConstraints(max_length=128, pattern=_IDENTIFIER)]

# Database object names used by verification assertions: "table", "schema.table",
# "db.schema.table" or Mongo "db.collection". Tools still quote and allowlist them (S19).
QualifiedName = Annotated[
    str,
    StringConstraints(
        max_length=256, pattern=r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*){0,2}$"
    ),
]
ColumnName = Annotated[str, StringConstraints(max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_$]*$")]
# S3 bucket naming rules (lower case, digits, dots and hyphens; 3-63 characters).
BucketName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")]

# Platform entity IDs are UUIDs (architecture §8).
AnalysisId = UUID
ReviewId = UUID
JobId = UUID
IncidentId = UUID
EventId = UUID

# Lower-case hex SHA-256, as produced by ``backend.domain.hashing.payload_hash``.
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

# Largest integer that JSON consumers using IEEE doubles (browsers, many viewers) show exactly.
MAX_SAFE_INTEGER = 2**53 - 1
