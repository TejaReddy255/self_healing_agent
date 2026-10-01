"""Task-log shaping for agents and the UI (S05): redact, then keep the tail.

Redaction runs on the whole downloaded text before truncation, so a secret is never cut in half at
the truncation boundary and left partly visible. The result is still untrusted text; prompts wrap
it in ``untrusted()`` (S08/S09).
"""

from __future__ import annotations

from backend.airflow.models import RawLog
from backend.observability.redaction import redact_text

TRUNCATION_MARKER = "[… truncated {count} chars …]\n"


def tail_and_redact(raw: RawLog, max_chars: int) -> tuple[str, bool, int]:
    """Return ``(content, truncated, original_chars)`` for a downloaded log."""
    redacted = redact_text(raw.text.replace("\x00", ""))
    original = len(redacted)
    truncated = raw.download_capped or original > max_chars
    if original <= max_chars:
        content = redacted
        dropped = 0
    else:
        content = redacted[-max_chars:]
        dropped = original - max_chars
    if truncated:
        content = TRUNCATION_MARKER.format(count=dropped if dropped else "earlier") + content
    return content, truncated, original
