"""Airflow 2.x stable REST client (S05).

- Basic auth from settings; redirects are never followed, so credentials cannot reach another host.
- Every path segment is URL-encoded on its own: run IDs contain ``:`` and ``+`` and may be hostile
  (``../``), and must reach Airflow as exactly one segment.
- Only GETs are retried (timeouts, connection errors, 5xx, 429). Trigger and pause are attempted
  once: writes are never retried automatically (NFR-011).
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from backend.airflow.errors import (
    AirflowAuthError,
    AirflowConflict,
    AirflowError,
    AirflowNotFound,
    AirflowProtocolError,
    AirflowRequestError,
    AirflowUnavailable,
)
from backend.airflow.models import (
    AirflowDag,
    AirflowDagRun,
    AirflowHealth,
    AirflowTaskInstance,
    DagListing,
    RawLog,
)
from backend.airflow.settings import AirflowSettings

logger = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
Sleep = Callable[[float], Awaitable[None]]

BACKOFF_BASE_S = 0.5
BACKOFF_CAP_S = 8.0
MAX_RETRY_AFTER_S = 30.0
TASK_PAGE_SIZE = 100
MAX_TASK_INSTANCES = 1_000
LOG_DOWNLOAD_CEILING_FACTOR = 16


class AirflowClient(Protocol):
    """What the platform needs from Airflow (ADR-012: fakes implement this too)."""

    async def health(self, *, retry: bool = True) -> AirflowHealth: ...

    async def get_dag(self, dag_id: str) -> AirflowDag: ...

    async def list_dags(self, dag_ids: list[str]) -> DagListing: ...

    async def patch_dag(self, dag_id: str, *, is_paused: bool) -> AirflowDag: ...

    async def list_dag_runs(self, dag_id: str, limit: int) -> list[AirflowDagRun]: ...

    async def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun: ...

    async def trigger_dag_run(
        self, dag_id: str, dag_run_id: str, conf: Mapping[str, Any] | None = None
    ) -> AirflowDagRun: ...

    async def list_task_instances(
        self, dag_id: str, dag_run_id: str
    ) -> list[AirflowTaskInstance]: ...

    async def get_task_log(
        self, dag_id: str, dag_run_id: str, task_id: str, try_number: int, map_index: int = -1
    ) -> RawLog: ...


def encode_path(*segments: str | int) -> str:
    """Join path segments, each fully percent-encoded (``/`` included)."""
    return "/".join(quote(str(segment), safe="") for segment in segments)


def _status_error(method: str, template: str, status: int) -> AirflowError:
    message = f"Airflow {method} {template} returned {status}"
    if status in (401, 403):
        return AirflowAuthError(message)
    if status == 404:
        return AirflowNotFound(message)
    if status == 409:
        return AirflowConflict(message)
    if status == 429 or status >= 500:
        return AirflowUnavailable(message, category=f"http_{status}")
    return AirflowRequestError(message)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(MAX_RETRY_AFTER_S, max(0.0, float(raw)))
    except ValueError:
        return None


class HttpAirflowClient:
    def __init__(
        self,
        settings: AirflowSettings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if settings.airflow_password is None or settings.airflow_username is None:
            raise AirflowUnavailable("Airflow is not configured", category="not configured")
        self._settings = settings
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=settings.api_root + "/",
            auth=httpx.BasicAuth(
                settings.airflow_username, settings.airflow_password.get_secret_value()
            ),
            timeout=httpx.Timeout(
                settings.airflow_read_timeout_s, connect=settings.airflow_connect_timeout_s
            ),
            follow_redirects=False,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- transport ------------------------------------------------------------------------

    def _backoff(self, attempt: int, response: httpx.Response | None) -> float:
        hinted = _retry_after(response) if response is not None else None
        if hinted is not None:
            return hinted
        jitter = random.uniform(0.5, 1.0)  # noqa: S311 - backoff jitter, not security
        return float(min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2.0**attempt) * jitter)

    async def _request(
        self,
        method: str,
        template: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        retries: int = 0,
    ) -> httpx.Response:
        attempt = 0
        while True:
            response: httpx.Response | None = None
            try:
                response = await self._client.request(method, path, params=params, json=json)
            except httpx.TimeoutException as exc:
                error: AirflowError = AirflowUnavailable(
                    f"Airflow {method} {template} timed out", category="timeout"
                )
                cause: BaseException | None = exc
            except httpx.TransportError as exc:
                error = AirflowUnavailable(f"Airflow {method} {template} unreachable")
                cause = exc
            else:
                if response.is_success:
                    return response
                if response.is_redirect:
                    error = AirflowRequestError(
                        f"Airflow {method} {template} redirected; redirects are not followed"
                    )
                else:
                    error = _status_error(method, template, response.status_code)
                cause = None
            if not isinstance(error, AirflowUnavailable) or attempt >= retries:
                raise error from cause
            delay = self._backoff(attempt, response)
            logger.warning(
                "airflow request failed; retrying",
                extra={
                    "method": method,
                    "path": template,
                    "attempt": attempt + 1,
                    "delay_s": delay,
                },
            )
            attempt += 1
            await self._sleep(delay)

    async def _get(self, template: str, path: str, **params: Any) -> Any:
        response = await self._request(
            "GET", template, path, params=params or None, retries=self._settings.airflow_get_retries
        )
        try:
            return response.json()
        except ValueError as exc:
            raise AirflowProtocolError(f"Airflow GET {template} returned invalid JSON") from exc

    @staticmethod
    def _parse(model: type[M], data: Any, template: str) -> M:
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise AirflowProtocolError(
                f"Airflow {template} response does not match {model.__name__}"
            ) from exc

    def _parse_list(self, model: type[M], data: Any, key: str, template: str) -> list[M]:
        items = data.get(key) if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise AirflowProtocolError(f"Airflow {template} response has no {key!r} list")
        return [self._parse(model, item, template) for item in items]

    # --- API ----------------------------------------------------------------------------------

    async def health(self, *, retry: bool = True) -> AirflowHealth:
        retries = self._settings.airflow_get_retries if retry else 0
        response = await self._request("GET", "/health", "health", retries=retries)
        try:
            data = response.json()
        except ValueError as exc:
            raise AirflowProtocolError("Airflow GET /health returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise AirflowProtocolError("Airflow GET /health returned an unexpected shape")
        return AirflowHealth(
            metadatabase=(data.get("metadatabase") or {}).get("status", "unknown"),
            scheduler=(data.get("scheduler") or {}).get("status", "unknown"),
            latest_scheduler_heartbeat=(data.get("scheduler") or {}).get(
                "latest_scheduler_heartbeat"
            ),
        )

    async def get_dag(self, dag_id: str) -> AirflowDag:
        template = "/dags/{dag_id}"
        return self._parse(
            AirflowDag, await self._get(template, encode_path("dags", dag_id)), template
        )

    async def list_dags(self, dag_ids: list[str]) -> DagListing:
        found: list[AirflowDag] = []
        missing: list[str] = []
        for dag_id in sorted(set(dag_ids)):
            try:
                found.append(await self.get_dag(dag_id))
            except AirflowNotFound:
                missing.append(dag_id)
        return DagListing(items=tuple(found), missing=tuple(missing))

    async def patch_dag(self, dag_id: str, *, is_paused: bool) -> AirflowDag:
        template = "/dags/{dag_id}"
        response = await self._request(
            "PATCH",
            template,
            encode_path("dags", dag_id),
            params={"update_mask": "is_paused"},
            json={"is_paused": is_paused},
        )
        return self._parse(AirflowDag, response.json(), template)

    async def list_dag_runs(self, dag_id: str, limit: int) -> list[AirflowDagRun]:
        template = "/dags/{dag_id}/dagRuns"
        data = await self._get(
            template,
            encode_path("dags", dag_id, "dagRuns"),
            limit=limit,
            order_by="-execution_date",
        )
        return self._parse_list(AirflowDagRun, data, "dag_runs", template)

    async def get_dag_run(self, dag_id: str, dag_run_id: str) -> AirflowDagRun:
        template = "/dags/{dag_id}/dagRuns/{dag_run_id}"
        data = await self._get(template, encode_path("dags", dag_id, "dagRuns", dag_run_id))
        return self._parse(AirflowDagRun, data, template)

    async def trigger_dag_run(
        self, dag_id: str, dag_run_id: str, conf: Mapping[str, Any] | None = None
    ) -> AirflowDagRun:
        template = "/dags/{dag_id}/dagRuns"
        response = await self._request(
            "POST",
            template,
            encode_path("dags", dag_id, "dagRuns"),
            json={"dag_run_id": dag_run_id, "conf": dict(conf or {})},
        )
        return self._parse(AirflowDagRun, response.json(), template)

    async def list_task_instances(self, dag_id: str, dag_run_id: str) -> list[AirflowTaskInstance]:
        template = "/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances"
        path = encode_path("dags", dag_id, "dagRuns", dag_run_id, "taskInstances")
        tasks: list[AirflowTaskInstance] = []
        offset = 0
        while offset < MAX_TASK_INSTANCES:
            data = await self._get(template, path, limit=TASK_PAGE_SIZE, offset=offset)
            page = self._parse_list(AirflowTaskInstance, data, "task_instances", template)
            tasks.extend(page)
            total = data.get("total_entries") if isinstance(data, dict) else None
            offset += len(page)
            if (
                not page
                or len(page) < TASK_PAGE_SIZE
                or (isinstance(total, int) and offset >= total)
            ):
                break
        return sorted(tasks, key=lambda t: (t.task_id, t.map_index))

    async def get_task_log(
        self, dag_id: str, dag_run_id: str, task_id: str, try_number: int, map_index: int = -1
    ) -> RawLog:
        template = "/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/{task_id}/logs/{try_number}"
        path = encode_path(
            "dags", dag_id, "dagRuns", dag_run_id, "taskInstances", task_id, "logs", try_number
        )
        params: dict[str, Any] = {"full_content": "true"}
        if map_index >= 0:
            params["map_index"] = map_index
        limit = self._settings.airflow_log_max_download_bytes
        attempt = 0
        while True:
            try:
                return await self._download_capped(template, path, params, limit)
            except AirflowUnavailable:
                if attempt >= self._settings.airflow_get_retries:
                    raise
                await self._sleep(self._backoff(attempt, None))
                attempt += 1

    async def _download_capped(
        self, template: str, path: str, params: Mapping[str, Any], limit: int
    ) -> RawLog:
        # Errors are at the end of a log, so keep a rolling tail of at most ``limit`` bytes in
        # memory; stop reading after LOG_DOWNLOAD_CEILING_FACTOR x limit to bound time and traffic.
        tail = bytearray()
        size = 0
        capped = False
        ceiling = limit * LOG_DOWNLOAD_CEILING_FACTOR
        try:
            async with self._client.stream(
                "GET", path, params=params, headers={"Accept": "text/plain"}
            ) as response:
                if not response.is_success:
                    raise _status_error("GET", template, response.status_code)
                async for chunk in response.aiter_bytes():
                    tail += chunk
                    size += len(chunk)
                    if len(tail) > limit:
                        del tail[: len(tail) - limit]
                    if size >= ceiling:
                        capped = True
                        break
        except httpx.TimeoutException as exc:
            raise AirflowUnavailable(
                f"Airflow GET {template} timed out", category="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise AirflowUnavailable(f"Airflow GET {template} unreachable") from exc
        text = bytes(tail).decode("utf-8", errors="replace")
        return RawLog(text=text, downloaded_bytes=size, download_capped=capped)
