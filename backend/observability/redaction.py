"""Secret redaction for log records and any other text that leaves the process (NFR-001)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "***REDACTED***"

SENSITIVE_KEY = re.compile(
    r"password|passwd|secret|token|api[_-]?key|authorization|credential|dsn|connection[_-]?string",
    re.IGNORECASE,
)

_INLINE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^:/\s@]+):[^@\s/]+@"), rf"\1:{REDACTED}@"),
    # HTTP auth schemes
    (re.compile(r"\b(Bearer)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), rf"\1 {REDACTED}"),
    (re.compile(r"\b(Basic)\s+[A-Za-z0-9+/=]{8,}", re.IGNORECASE), rf"\1 {REDACTED}"),
    # key=value / key: value pairs in free text
    (
        re.compile(
            r"(?i)\b(password|passwd|secret|token|api[_-]?key|client[_-]?secret)"
            r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
        ),
        rf"\1\2{REDACTED}",
    ),
)


def redact_text(text: str) -> str:
    """Mask inline credential patterns in free text."""
    for pattern, replacement in _INLINE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact a structured value; values under sensitive keys are fully masked."""
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(v) for v in value]
    return value
