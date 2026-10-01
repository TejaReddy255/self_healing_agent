"""Explicit, default-deny tool-permission registry (S06, ADR-005).

The registry is the only classifier. The name heuristic and the server's ``destructiveHint`` are
tripwires that can only *exclude* a tool registered as read; they never grant anything. A missing
or invalid registry stops startup (decision 2026-10-01).
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

# Tripwire vocabulary, matched against whole name tokens (split on "_", ".", "-" and camelCase)
# so read tools such as "describe_dataset" are not flagged. Long verbs also match as a token
# prefix ("insertRows" -> "insertrows").
_SHORT_WRITE_WORDS = frozenset({"put", "set", "drop", "copy", "exec"})
_LONG_WRITE_VERBS = (
    "execute", "insert", "update", "upsert", "delete", "create", "alter", "upload", "remove",
    "truncate", "merge", "grant", "revoke", "write", "replace", "rename",
)  # fmt: skip
_TOKEN_SPLIT = re.compile(r"[_.\-]+|(?<=[a-z0-9])(?=[A-Z])")


def looks_write_capable(name: str) -> bool:
    tokens = [t.lower() for t in _TOKEN_SPLIT.split(name) if t]
    return any(
        token in _SHORT_WRITE_WORDS or token.startswith(_LONG_WRITE_VERBS) for token in tokens
    )


ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,128}$")]


class PermissionEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server: SystemName
    tool: ToolName
    access: Literal["read", "write"]
    timeout_s: Annotated[float, Field(gt=0, le=600)] | None = None
    max_output_bytes: Annotated[int, Field(ge=256, le=1_048_576)] | None = None
    input_schema_sha256: Sha256Hex | None = None
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


def schema_sha256(schema: dict[str, object]) -> str:
    """SHA-256 of a tool's canonical JSON input schema (what a registry entry may pin)."""
    try:
        return hashlib.sha256(canonical_json(schema)).hexdigest()
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

        def result(tool_class: ToolClass, reason: str | None = None) -> ClassifiedTool:
            timeout = entry.timeout_s if entry and entry.timeout_s else default_timeout_s
            cap = default_max_output_bytes
            if entry and entry.max_output_bytes:
                cap = min(cap, entry.max_output_bytes)
            return ClassifiedTool(server, spec, tool_class, timeout, cap, reason)

        if entry is None:
            return result(ToolClass.UNCLASSIFIED, "not in the permission registry")
        if (
            entry.input_schema_sha256 is not None
            and schema_sha256(spec.input_schema) != entry.input_schema_sha256
        ):
            logger.warning(
                "mcp tool schema changed; tool excluded",
                extra={"server": server.value, "tool": spec.name},
            )
            return result(ToolClass.SCHEMA_CHANGED, "input schema differs from the pinned hash")
        if entry.access == "read" and (
            looks_write_capable(spec.name) or spec.destructive_hint is True
        ):
            logger.warning(
                "mcp tool registered as read looks write-capable; tool excluded",
                extra={"server": server.value, "tool": spec.name},
            )
            return result(ToolClass.MISMATCH, "registered as read but looks write-capable")
        return result(ToolClass.WRITE if entry.access == "write" else ToolClass.READ)
