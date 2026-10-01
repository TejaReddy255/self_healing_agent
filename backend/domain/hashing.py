"""Canonical JSON and the approval-payload hash (src §9.2, ADR-006).

The hash is the approval boundary: a reviewer's approval binds to exactly these bytes. Changing any
rule below (or the field set of ``ApprovalPayload`` and its nested models, because ``None`` fields
are serialised) invalidates stored approvals, so it requires an ADR.

Rules:
- models are dumped with ``model_dump(mode="json", exclude_none=False)``. The Python-mode dump is
  canonicalised too and must give the same result, so values pydantic would silently coerce in JSON
  mode (NaN to null, sets to lists, int keys to strings) are rejected instead of hashed;
- object keys are sorted, separators are ``(",", ":")``;
- output is UTF-8 with ``ensure_ascii=False``;
- NaN and Infinity are rejected;
- datetimes must be timezone-aware and are written as ISO-8601 UTC with a ``Z`` suffix;
- Decimals are written as strings; UUIDs as strings; enums by value;
- object keys must be strings, and sets (which have no order) are rejected.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from backend.domain.review import ApprovalPayload
from backend.domain.sop import SOPProposal


class CanonicalizationError(ValueError):
    """The value has no single canonical JSON form."""


def _normalise(value: Any, path: str) -> Any:
    if isinstance(value, BaseModel):
        dumped = _normalise(value.model_dump(mode="json", exclude_none=False), path)
        if _normalise(value.model_dump(mode="python", exclude_none=False), path) != dumped:
            raise CanonicalizationError(f"{path}: model contains values without a canonical form")
        return dumped
    if value is None or isinstance(value, (bool, int, str)):
        return value.value if isinstance(value, Enum) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(f"{path}: NaN and Infinity have no canonical form")
        return value
    if isinstance(value, Enum):
        return _normalise(value.value, path)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings, got {key!r}")
            result[key] = _normalise(item, f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_normalise(item, f"{path}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise CanonicalizationError(f"{path}: datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError(f"{path}: NaN and Infinity have no canonical form")
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    raise CanonicalizationError(f"{path}: {type(value).__name__} has no canonical JSON form")


def canonical_json(obj: Any) -> bytes:
    """Serialise ``obj`` (a model or JSON-like value) to its canonical UTF-8 JSON bytes."""
    text = json.dumps(
        _normalise(obj, "$"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:  # lone surrogates cannot be encoded
        raise CanonicalizationError("strings must be valid Unicode (no lone surrogates)") from exc


def payload_hash(payload: ApprovalPayload) -> str:
    """Lower-case hex SHA-256 of the canonical approval payload."""
    return hashlib.sha256(canonical_json(payload)).hexdigest()


# Fields that define what a SOP makes an operator or agent do (ADR-016). Catalogue fields
# (status, version, tags, pipeline_ids) are excluded: editing them must not invalidate approvals,
# and deprecation is checked explicitly at PRECHECK instead.
SOP_HASH_FIELDS = (
    "title",
    "category",
    "target_system",
    "risk_level",
    "required_evidence",
    "remediation_steps",
    "verification_steps",
    "stop_conditions",
)
SOP_HASH_VERSION = 1


def sop_content_sha256(sop_id: str, sop: SOPProposal) -> str:
    """The pin stored in ``SOPReference.content_sha256``, identical for a static YAML SOP and a
    runtime-approved SOP (S10) with the same behavioural content."""
    content = sop.model_dump(mode="json", include=set(SOP_HASH_FIELDS))
    document = {"sop_hash_version": SOP_HASH_VERSION, "sop_id": sop_id, **content}
    return hashlib.sha256(canonical_json(document)).hexdigest()
