"""Secret redaction for log records and any other text that leaves the process (NFR-001)."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

REDACTED = "***REDACTED***"

SENSITIVE_KEY = re.compile(
    r"password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|fernet[_-]?key"
    r"|authorization|credential|dsn|connection[_-]?string|cookie|session",
    re.IGNORECASE,
)

# "secret" also covers aws_secret_access_key, client_secret and secret_key.
_INLINE_KEYWORDS = (
    r"password|passwd|passphrase|secret|token|api[_-]?key|private[_-]?key|fernet[_-]?key"
    r"|authorization|credentials?|dsn|connection[_-]?string|cookie"
)

# Every repetition below is bounded or possessive and every pattern only starts at a token boundary,
# so redaction stays linear even on a single multi-megabyte line (hex dumps in task logs). Patterns
# whose marker substring is absent are skipped entirely.
_KEYWORD = re.compile(rf"(?i)(?:{_INLINE_KEYWORDS})")

# (marker that must appear in the lower-cased text, pattern, replacement)
_INLINE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    # PEM private key blocks
    (
        "private key-----",
        re.compile(
            r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----.{0,20000}?"
            r"-----END [A-Z ]{0,40}PRIVATE KEY-----",
            re.DOTALL,
        ),
        REDACTED,
    ),
    # Credentials embedded in URLs: scheme://user:password@host (user may be empty). The password
    # is matched greedily up to the last "@" of the token, so passwords containing "@" or "/" are
    # fully masked.
    (
        "://",
        re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]{0,31}+://[^:/\s@]{0,256}+):\S{0,1024}@"),
        rf"\1:{REDACTED}@",
    ),
    # AWS access key IDs and JSON Web Tokens appear bare in logs.
    ("a", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
    (
        "eyj",
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{4,4096}+\.eyJ[A-Za-z0-9_-]{4,8192}+\.[A-Za-z0-9_-]{0,2048}+"
        ),
        REDACTED,
    ),
    # HTTP auth schemes (masked before the key=value rule sees "Authorization:")
    (
        "bearer",
        re.compile(r"\b(Bearer)\s{1,8}[A-Za-z0-9._~+/=-]{1,8192}+", re.IGNORECASE),
        rf"\1 {REDACTED}",
    ),
    (
        "basic",
        re.compile(r"\b(Basic)\s{1,8}[A-Za-z0-9+/=]{8,8192}+", re.IGNORECASE),
        rf"\1 {REDACTED}",
    ),
)

# key=value / key: value pairs, including prefixed and suffixed keys (PG_PASSWORD, access_token,
# aws_secret_access_key) and quoted dict/JSON keys ("token": "x"). The key is one whole token, so
# matching starts only where a token starts; a callback decides whether it names a secret.
_KEY_VALUE = re.compile(
    r"(?<![A-Za-z0-9_.-])([\"']?)([A-Za-z0-9_.-]{1,128}+)([\"']?)(\s{0,8}[=:]\s{0,8})"
    r"(\"[^\"]{0,4096}\"|'[^']{0,4096}'|[^\s,;&}\]]{1,4096}+)"
)


def _mask_key_value(match: re.Match[str]) -> str:
    if not _KEYWORD.search(match.group(2)):
        return match.group(0)
    return f"{match.group(1)}{match.group(2)}{match.group(3)}{match.group(4)}{REDACTED}"


def redact_text(text: str) -> str:
    """Mask inline credential patterns in free text."""
    lowered = text.lower()
    for marker, pattern, replacement in _INLINE_PATTERNS:
        if marker in lowered:
            text = pattern.sub(replacement, text)
    if _KEYWORD.search(text):
        text = _KEY_VALUE.sub(_mask_key_value, text)
    return text


def redact_value(value: Any, *, key: str | None = None) -> Any:
    """Recursively redact a structured value; values under sensitive keys are fully masked.

    Objects that are not JSON primitives are rendered to text first and then redacted, so their
    string form (e.g. an exception or URL object) cannot reach a log unmasked.
    """
    if key is not None and SENSITIVE_KEY.search(key):
        return REDACTED
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [redact_value(v) for v in value]
    return redact_text(str(value))
