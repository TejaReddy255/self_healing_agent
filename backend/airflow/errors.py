"""Airflow client errors (S05).

Messages carry the method, path template and status only: never response bodies (which may echo
request data) or credentials. ``category`` is the short label used in runtime context and logs.
"""

from __future__ import annotations


class AirflowError(Exception):
    category = "error"

    def __init__(self, message: str, *, category: str | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category


class AirflowUnavailable(AirflowError):
    """Timeout, connection failure, 5xx or 429 (after retries for GETs), or not configured."""

    category = "unreachable"

    def __init__(
        self, message: str, *, category: str | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(message, category=category)
        self.retry_after = retry_after


class AirflowNotFound(AirflowError):
    category = "not_found"


class AirflowConflict(AirflowError):
    category = "conflict"


class AirflowAuthError(AirflowError):
    """401/403: the platform's Airflow credentials are wrong or lack permission."""

    category = "auth_failed"


class AirflowRequestError(AirflowError):
    """Another 4xx: the request was rejected."""

    category = "bad_request"


class AirflowProtocolError(AirflowError):
    """The response did not match the expected Airflow API shape."""

    category = "bad_response"


class AirflowToolDenied(AirflowError):
    """An agent tool was asked about a DAG outside its allowlist (no Airflow call was made)."""

    category = "denied"
