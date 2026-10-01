"""JSON-safe, bounded tool results (S07, NFR-006)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any
from uuid import UUID

MAX_TEXT_CHARS = 256


def safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return (
            value if value == value and value not in (float("inf"), float("-inf")) else str(value)
        )
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    text = str(value).replace("\x00", "")
    return text if len(text) <= MAX_TEXT_CHARS else text[:MAX_TEXT_CHARS] + "…"


def table_result(columns: Sequence[str], rows: Sequence[Sequence[Any]], limit: int) -> str:
    """``limit + 1`` rows may be passed: the extra one only signals truncation."""
    truncated = len(rows) > limit
    kept = [[safe_value(v) for v in row] for row in rows[:limit]]
    return dumps(
        {
            "columns": list(columns),
            "rows": kept,
            "row_count": len(kept),
            "truncated": truncated,
            "limit": limit,
        }
    )


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
