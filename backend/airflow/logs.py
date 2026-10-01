"""Task-log shaping for agents and the UI (S05): clean, redact, then keep the tail.

Order matters:
1. Drop the first partial line when the download window started mid-log, so a secret cut at the
   window boundary is not left half-visible.
2. Strip ANSI escapes and control characters (except newline and tab).
3. Cut to the character limit plus a margin of whole lines, then redact: redaction never sees more
   than about ``max_chars`` (bounded work) and still sees every secret near the cut in full.
4. Keep the last ``max_chars`` characters.

The result is still untrusted text; prompts wrap it in ``untrusted()`` (S08/S09).
"""

from __future__ import annotations

import re

from backend.airflow.models import RawLog
from backend.observability.redaction import redact_text

TRUNCATED_HEAD = "[… truncated {count} chars …]\n"
DOWNLOAD_LIMIT_NOTE = (
    "\n[… the log continues past the download limit: this is NOT the end of the log …]"
)
# Whole lines before the cut are redacted too, so a secret spanning the cut is masked first.
REDACTION_MARGIN_CHARS = 4_096

_ANSI = re.compile(r"\x1b\[[0-9;?]{0,32}[ -/]{0,8}[@-~]|\x1b[@-Z\\-_]")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: str) -> str:
    return _CONTROL.sub("", _ANSI.sub("", text))


def tail_and_redact(raw: RawLog, max_chars: int) -> tuple[str, bool, int]:
    """Return ``(content, truncated, original_chars)`` for a downloaded log."""
    text = raw.text
    if raw.tail_trimmed and "\n" in text:
        text = text.split("\n", 1)[1]
    text = clean(text)
    original = len(text)
    window = text
    if len(window) > max_chars + REDACTION_MARGIN_CHARS:
        window = window[-(max_chars + REDACTION_MARGIN_CHARS) :]
        if "\n" in window:
            window = window.split("\n", 1)[1]
    redacted = redact_text(window)
    dropped = max(0, original - len(window)) + max(0, len(redacted) - max_chars)
    content = redacted[-max_chars:] if len(redacted) > max_chars else redacted
    truncated = raw.tail_trimmed or raw.download_capped or dropped > 0
    if truncated:
        content = TRUNCATED_HEAD.format(count=dropped if dropped else "earlier") + content
    if raw.download_capped:
        content += DOWNLOAD_LIMIT_NOTE
    return content, truncated, original
