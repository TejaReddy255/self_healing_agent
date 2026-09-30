"""Structured JSON logging with request correlation and redaction (S01).

The redaction filter is attached to every handler created here. Filters on a logger only see
records logged directly on that logger, so handler-level filters are what guarantee coverage of
records propagated from child loggers.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from backend.observability.context import current_request_id
from backend.observability.redaction import redact_text, redact_value

# Attributes present on every LogRecord; anything else was passed via ``extra=``.
# ``color_message`` is uvicorn's ANSI-coloured duplicate of the message; drop it from JSON.
_STANDARD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "color_message"}
)

# Loggers configured by uvicorn before our app starts; route them through our handler.
_UVICORN_LOGGERS: Final = ("uvicorn", "uvicorn.error", "uvicorn.access")


class RedactionFilter(logging.Filter):
    """Redacts the rendered message and every ``extra`` field on the record.

    Every handler must be created with :func:`build_handler` so it carries this filter; handlers
    added any other way (e.g. OTel/Sentry in S23) would receive unredacted records.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            # Filters run outside logging's own error handling: a malformed call such as
            # logger.warning("%s %s", 1) must not raise into application code.
            message = f"{record.msg} (log formatting failed; args dropped)"
        record.msg = redact_text(message)
        record.args = None
        for attr in list(vars(record)):
            if attr not in _STANDARD_ATTRS:
                setattr(record, attr, redact_value(getattr(record, attr), key=attr))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }
        for attr, value in vars(record).items():
            if attr not in _STANDARD_ATTRS and attr not in payload:
                payload[attr] = value
        if record.exc_info:
            payload["exc_info"] = redact_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


def build_handler(stream: Any = None) -> logging.Handler:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactionFilter())
    return handler


def configure_logging(level: str) -> None:
    """Install a single JSON handler on the root logger and route uvicorn logs through it."""
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(build_handler())
    root.setLevel(level)

    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True
    # Per-request access lines come from our middleware (with request_id); silence the duplicate.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
