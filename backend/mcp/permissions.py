"""Explicit, default-deny tool-permission registry (S06, ADR-005).

The registry is the only classifier. Every entry pins the SHA-256 of the tool's full definition
(name, description, input schema, annotations); a different live definition is SCHEMA_CHANGED and
excluded (decision 2026-10-01). Tripwires can only *exclude* a tool registered as read, never grant
anything: a write-looking name, ``destructiveHint=true``, or a missing ``readOnlyHint=true``
(decision 2026-10-01). A missing or invalid registry stops startup.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from backend.config import ConfigurationError
from backend.domain.common import Sha256Hex, SystemName
from backend.domain.hashing import CanonicalizationError, canonical_json
from backend.mcp.models import ClassifiedTool, ToolClass, ToolSpec
from backend.yaml_safe import StrictYamlError, load_strict

logger = logging.getLogger(__name__)

# Tripwire (decision 2026-10-01, hybrid): long write verbs match anywhere in the lower-cased
# name ("bulkwrite", "SQLExecute", "softdelete"); short words must be whole tokens, so read tools
# such as "describe_dataset" are not flagged.
_LONG_WRITE_VERBS = (
    "execute", "insert", "update", "upsert", "delete", "create", "alter", "upload", "remove",
    "truncate", "merge", "grant", "revoke", "write", "replace", "rename", "reset", "purge",
    "destroy", "modify",
)  # fmt: skip
_SHORT_WRITE_WORDS = frozenset(
    {"put", "set", "copy", "drop", "exec", "move", "patch", "post", "kill"}
)
_TOKEN_SPLIT = re.compile(r"[_.\-\s]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def looks_write_capable(name: str) -> bool:
    lowered = name.lower()
    if any(verb in lowered for verb in _LONG_WRITE_VERBS):
        return True
    tokens = [t.lower() for t in _TOKEN_SPLIT.split(name) if t]
    return any(token in _SHORT_WRITE_WORDS for token in tokens)


class PermissionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server: SystemName
    tool: ToolName
    access: Literal["read", "write"]
    timeout_s: Annotated[float, Field(gt=0, le=600)] | None = None
    max_output_bytes: Annotated[int, Field(ge=256, le=1_048_576)] | None = None
    # Mandatory (decision 2026-10-01): pins the whole definition the reviewer vetted.
    definition_sha256: Sha256Hex
    notes: str | None = None


class RegistryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    tools: tuple[PermissionEntry, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> Self:
        seen: set[tuple[SystemName, str]] = set()
        for entry in self.tools:
            key = (entry.server, entry.tool)
            if key in seen:
                raise ValueError(f"duplicate registry entry for {entry.server.value}.{entry.tool}")
            seen.add(key)
        return self


def definition_sha256(spec: ToolSpec) -> str:
    """SHA-256 of a tool's canonical definition: what every registry entry pins.

    The description is included because it reaches prompts (tool poisoning); the annotations
    because they carry the read-only/destructive hints.
    """
    document = {
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
        "annotations": spec.annotations,
    }
    try:
        return hashlib.sha256(canonical_json(document)).hexdigest()
    except CanonicalizationError:
        return "unhashable"


class ToolPermissionRegistry:
    def __init__(self, registry: RegistryFile) -> None:
        self._entries = {(e.server, e.tool): e for e in registry.tools}

    @classmethod
    def load(cls, path: Path) -> ToolPermissionRegistry:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise ConfigurationError(f"cannot read TOOL_PERMISSIONS_FILE {path.name}") from exc
        try:
            data = load_strict(text)
            registry = RegistryFile.model_validate(data)
        except StrictYamlError as exc:
            raise ConfigurationError(f"invalid TOOL_PERMISSIONS_FILE: {exc.message}") from None
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'file'}: {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            )
            raise ConfigurationError(f"invalid TOOL_PERMISSIONS_FILE: {details}") from None
        return cls(registry)

    def __len__(self) -> int:
        return len(self._entries)

    def classify(
        self,
        server: SystemName,
        spec: ToolSpec,
        *,
        default_timeout_s: float,
        default_max_output_bytes: int,
    ) -> ClassifiedTool:
        entry = self._entries.get((server, spec.name))
        live_hash = definition_sha256(spec)

        def result(tool_class: ToolClass, reason: str | None = None) -> ClassifiedTool:
            timeout = entry.timeout_s if entry and entry.timeout_s else default_timeout_s
            cap = default_max_output_bytes
            if entry and entry.max_output_bytes:
                cap = min(cap, entry.max_output_bytes)
            return ClassifiedTool(server, spec, tool_class, timeout, cap, reason, live_hash)

        if entry is None:
            return result(ToolClass.UNCLASSIFIED, "not in the permission registry")
        if live_hash != entry.definition_sha256:
            logger.warning(
                "mcp tool definition changed; tool excluded",
                extra={"server": server.value, "tool": spec.name},
            )
            return result(ToolClass.SCHEMA_CHANGED, "definition differs from the pinned hash")
        if entry.access == "read":
            reason = None
            if looks_write_capable(spec.name):
                reason = "registered as read but its name looks write-capable"
            elif spec.destructive_hint is True:
                reason = "registered as read but the server marks it destructive"
            elif spec.read_only_hint is not True:
                reason = "registered as read but the server does not mark it readOnlyHint"
            if reason is not None:
                logger.warning(
                    "mcp read tool failed a tripwire; tool excluded",
                    extra={"server": server.value, "tool": spec.name, "reason": reason},
                )
                return result(ToolClass.MISMATCH, reason)
        return result(ToolClass.WRITE if entry.access == "write" else ToolClass.READ)
