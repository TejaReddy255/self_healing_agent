"""Discover and parse metadata files safely (S04).

Only this package reads ``METADATA_DIR``. Parsing is hardened against hostile or careless input:
- YAML goes through ``backend.yaml_safe`` (no tags, anchors, duplicate keys or deep nesting);
- JSON rejects duplicate keys and NaN/Infinity;
- files are size-capped and must resolve inside the metadata directory (no symlink escapes).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.metadata.models import DocumentType, IssueSeverity, ValidationIssue
from backend.yaml_safe import MAX_NESTING_DEPTH, StrictYamlError, load_strict

TYPE_DIRECTORIES: dict[str, DocumentType] = {
    "pipelines": DocumentType.PIPELINE,
    "dq_rules": DocumentType.BUSINESS_RULE,
    "sops": DocumentType.SOP,
    "runbooks": DocumentType.RUNBOOK,
    "known_issues": DocumentType.KNOWN_ISSUE,
    "ownership": DocumentType.OWNERSHIP,
    "historical_incidents": DocumentType.HISTORICAL_INCIDENT,
}
DOCUMENT_SUFFIXES = frozenset({".yaml", ".yml", ".json"})
IGNORED_NAMES = frozenset({"README.md"})


class MetadataParseError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    document_type: DocumentType
    size: int


@dataclass(frozen=True)
class Discovery:
    files: tuple[SourceFile, ...]
    issues: tuple[ValidationIssue, ...]


def _issue(code: str, message: str, file: str, severity: IssueSeverity) -> ValidationIssue:
    return ValidationIssue(severity=severity, code=code, message=message, file=file)


def discover(root: Path) -> Discovery:
    """List loadable files under ``root`` in a stable order, with discovery issues."""
    root = root.resolve()
    if not root.is_dir():
        missing = _issue(
            "YAML_INVALID", "metadata directory does not exist", str(root.name), IssueSeverity.ERROR
        )
        return Discovery((), (missing,))
    files: list[SourceFile] = []
    issues: list[ValidationIssue] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.name in IGNORED_NAMES or any(
            part.startswith(".") for part in path.relative_to(root).parts
        ):
            continue  # README files and hidden entries such as .git/
        if path.is_symlink() or path.is_file():
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                issues.append(
                    _issue(
                        "PATH_ESCAPE",
                        "file resolves outside the metadata directory",
                        relative,
                        IssueSeverity.ERROR,
                    )
                )
                continue
            if not resolved.is_file():
                continue
            top = relative.split("/", 1)[0]
            document_type = TYPE_DIRECTORIES.get(top)
            if "/" not in relative or document_type is None or path.suffix not in DOCUMENT_SUFFIXES:
                issues.append(
                    _issue(
                        "UNKNOWN_FILE",
                        "not a metadata document; ignored",
                        relative,
                        IssueSeverity.WARNING,
                    )
                )
                continue
            files.append(SourceFile(path, relative, document_type, resolved.stat().st_size))
    return Discovery(tuple(files), tuple(issues))


OVERSIZED = "oversized"
UNREADABLE = "unreadable"


def read_capped(source: SourceFile, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` (+1 to detect growth since discovery). Callers hash and parse
    these same bytes, so a snapshot's version always describes what it parsed."""
    if source.size > max_bytes:
        raise MetadataParseError(
            "FILE_TOO_LARGE", f"file is {source.size} bytes; the limit is {max_bytes}"
        )
    with source.path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise MetadataParseError("FILE_TOO_LARGE", f"file exceeds {max_bytes} bytes")
    return data


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_version_hash(source: SourceFile, max_bytes: int) -> str:
    """The per-file hash used in tree versions, matching what ``build_candidate`` records."""
    try:
        return content_hash(read_capped(source, max_bytes))
    except MetadataParseError:
        return OVERSIZED
    except OSError:
        return UNREADABLE


def tree_version(file_hashes: dict[str, str]) -> str:
    """SHA-256 over sorted ``(relative path, file hash)`` pairs."""
    digest = hashlib.sha256()
    for relative in sorted(file_hashes):
        digest.update(f"{relative}\0{file_hashes[relative]}\n".encode())
    return digest.hexdigest()


def _reject_constant(value: str) -> None:
    raise MetadataParseError("YAML_INVALID", f"{value} is not allowed in JSON documents")


def _no_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MetadataParseError("YAML_INVALID", f"duplicate key {key!r}")
        result[key] = value
    return result


def _too_deep(value: Any) -> bool:
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > MAX_NESTING_DEPTH:
            return True
        if isinstance(item, dict):
            stack.extend((v, depth + 1) for v in item.values())
        elif isinstance(item, list):
            stack.extend((v, depth + 1) for v in item)
    return False


def parse(source: SourceFile, data: bytes) -> dict[str, Any]:
    """Parse one document; the top level must be a mapping."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MetadataParseError("YAML_INVALID", "file is not valid UTF-8") from exc
    try:
        if source.path.suffix == ".json":
            parsed = json.loads(
                text, object_pairs_hook=_no_duplicate_pairs, parse_constant=_reject_constant
            )
        else:
            parsed = load_strict(text)
    except MetadataParseError:
        raise
    except StrictYamlError as exc:
        raise MetadataParseError(exc.code, exc.message) from exc
    except json.JSONDecodeError as exc:
        raise MetadataParseError("YAML_INVALID", f"invalid JSON (line {exc.lineno})") from exc
    except RecursionError as exc:
        raise MetadataParseError(
            "YAML_INVALID", f"nesting deeper than {MAX_NESTING_DEPTH} levels"
        ) from exc
    if _too_deep(parsed):
        raise MetadataParseError("YAML_INVALID", f"nesting deeper than {MAX_NESTING_DEPTH} levels")
    if not isinstance(parsed, dict):
        raise MetadataParseError("YAML_INVALID", "the top level must be a mapping")
    return parsed
