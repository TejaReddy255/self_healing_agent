"""A fake Airflow 2.x stable REST API (``/api/v1``) for tests, built on respx (S05).

Responses follow the Airflow 2.x OpenAPI shapes the platform uses. Downstream slices build
scenarios with ``add_dag`` / ``add_run`` / ``add_task`` / ``set_log`` and inject failures with
``fail_next``. Every request is recorded in ``calls`` with its raw (still percent-encoded) path.
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx
import respx
from pydantic import SecretStr

from backend.airflow.client import HttpAirflowClient
from backend.airflow.settings import AirflowSettings

BASE_URL = "http://airflow.test"
API_PREFIX = "/api/v1/"
USERNAME = "svc-selfhealing"
PASSWORD = "airflow-pass-do-not-leak-7731"


def fake_settings(**overrides: Any) -> AirflowSettings:
    values: dict[str, Any] = {
        "airflow_base_url": BASE_URL,
        "airflow_username": USERNAME,
        "airflow_password": SecretStr(PASSWORD),
    }
    values.update(overrides)
    return AirflowSettings(_env_file=None, **values)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class Call:
    method: str
    raw_path: str
    params: dict[str, str]
    body: Any = None


@dataclass
class _Failure:
    method: str
    path_contains: str
    kind: str | int
    times: int
    retry_after: str | None = None


@dataclass
class FakeAirflow:
    dags: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    tasks: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    logs: dict[tuple[str, str, str, int, int], str] = field(default_factory=dict)
    health: dict[str, Any] = field(
        default_factory=lambda: {
            "metadatabase": {"status": "healthy"},
            "scheduler": {"status": "healthy", "latest_scheduler_heartbeat": None},
        }
    )
    calls: list[Call] = field(default_factory=list)
    failures: list[_Failure] = field(default_factory=list)
    # Airflow's [api] maximum_page_limit: pages are never larger than this.
    max_page_limit: int = 100
    # Serve logs in chunks of this many bytes (exercises streaming); None = one chunk.
    log_chunk_size: int | None = None

    # --- scenario builders ------------------------------------------------------------------

    def add_dag(self, dag_id: str, *, is_paused: bool = False, **extra: Any) -> FakeAirflow:
        self.dags[dag_id] = {
            "dag_id": dag_id,
            "is_paused": is_paused,
            "is_active": True,
            "schedule_interval": {"__type": "CronExpression", "value": "0 2 * * *"},
            "timetable_description": "At 02:00",
            "next_dagrun": "2026-10-02T02:00:00+00:00",
            "next_dagrun_data_interval_start": "2026-10-01T02:00:00+00:00",
            "next_dagrun_data_interval_end": "2026-10-02T02:00:00+00:00",
            "tags": [{"name": "customer"}],
            "owners": ["data-eng"],
        } | extra
        self.runs.setdefault(dag_id, {})
        return self

    def add_run(
        self,
        dag_id: str,
        run_id: str,
        *,
        state: str = "success",
        logical_date: datetime | None = None,
        conf: dict[str, Any] | None = None,
    ) -> FakeAirflow:
        logical = logical_date or datetime(2026, 10, 1, 2, tzinfo=UTC)
        self.runs.setdefault(dag_id, {})[run_id] = {
            "dag_id": dag_id,
            "dag_run_id": run_id,
            "state": state,
            "run_type": "scheduled",
            "logical_date": _iso(logical),
            "execution_date": _iso(logical),
            "data_interval_start": _iso(logical),
            "data_interval_end": _iso(logical),
            "start_date": _iso(logical),
            "end_date": _iso(logical) if state in ("success", "failed") else None,
            "external_trigger": False,
            "conf": conf or {},
            "note": None,
        }
        return self

    def add_task(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        *,
        state: str = "success",
        try_number: int = 1,
        map_index: int = -1,
    ) -> FakeAirflow:
        self.tasks.setdefault((dag_id, run_id), []).append(
            {
                "task_id": task_id,
                "dag_id": dag_id,
                "dag_run_id": run_id,
                "map_index": map_index,
                "state": state,
                "try_number": try_number,
                "start_date": "2026-10-01T02:01:00+00:00",
                "end_date": "2026-10-01T02:04:00+00:00",
                "duration": 180.0,
                "operator": "PythonOperator",
                "hostname": "worker-1",
            }
        )
        return self

    def set_log(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        try_number: int,
        text: str,
        *,
        map_index: int = -1,
    ) -> FakeAirflow:
        self.logs[(dag_id, run_id, task_id, try_number, map_index)] = text
        return self

    def fail_next(
        self,
        method: str,
        path_contains: str,
        kind: str | int,
        *,
        times: int = 1,
        retry_after: str | None = None,
    ) -> FakeAirflow:
        """Make the next ``times`` matching requests fail: an HTTP status or "timeout"/"connect"."""
        self.failures.append(_Failure(method, path_contains, kind, times, retry_after))
        return self

    def calls_to(self, method: str, path_contains: str = "") -> list[Call]:
        return [c for c in self.calls if c.method == method and path_contains in c.raw_path]

    # --- transport ------------------------------------------------------------------------------

    def _failure_for(self, request: httpx.Request) -> _Failure | None:
        for failure in self.failures:
            if (
                failure.times > 0
                and failure.method == request.method
                and failure.path_contains in request.url.raw_path.decode()
            ):
                failure.times -= 1
                return failure
        return None

    def handle(self, request: httpx.Request) -> httpx.Response:
        raw_path = request.url.raw_path.decode().split("?", 1)[0]
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append(Call(request.method, raw_path, params, body))

        expected = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"title": "Unauthorized"})

        failure = self._failure_for(request)
        if failure is not None:
            if failure.kind == "timeout":
                raise httpx.ReadTimeout("simulated timeout", request=request)
            if failure.kind == "connect":
                raise httpx.ConnectError("simulated connection failure", request=request)
            headers = {"Retry-After": failure.retry_after} if failure.retry_after else {}
            return httpx.Response(
                int(failure.kind),
                json={"title": "failure", "detail": f"echo {PASSWORD}"},  # must never be echoed
                headers=headers,
            )

        assert raw_path.startswith(API_PREFIX), raw_path
        segments = [unquote(s) for s in raw_path[len(API_PREFIX) :].split("/")]
        return self._route(request.method, segments, params, body, request.headers)

    def _route(
        self,
        method: str,
        s: list[str],
        params: dict[str, str],
        body: Any,
        headers: httpx.Headers,
    ) -> httpx.Response:
        not_found = httpx.Response(404, json={"title": "Not Found", "status": 404})
        if s == ["health"] and method == "GET":
            return httpx.Response(200, json=self.health)
        if len(s) < 2 or s[0] != "dags" or s[1] not in self.dags:
            return not_found
        dag_id = s[1]
        dag = self.dags[dag_id]
        if len(s) == 2:
            if method == "GET":
                return httpx.Response(200, json=dag)
            if method == "PATCH":
                dag["is_paused"] = bool(body["is_paused"])
                return httpx.Response(200, json=dag)
        if len(s) == 3 and s[2] == "dagRuns":
            runs = self.runs[dag_id]
            if method == "GET":
                # Real Airflow orders by id (insertion) unless order_by is given.
                if params.get("order_by") == "-execution_date":
                    ordered = sorted(runs.values(), key=lambda r: r["logical_date"], reverse=True)
                else:
                    ordered = list(runs.values())
                limit = min(int(params.get("limit", 25)), self.max_page_limit)
                return httpx.Response(
                    200, json={"dag_runs": ordered[:limit], "total_entries": len(ordered)}
                )
            if method == "POST":
                run_id = body["dag_run_id"]
                if run_id in runs:
                    return httpx.Response(409, json={"title": "Conflict", "status": 409})
                self.add_run(
                    dag_id,
                    run_id,
                    state="queued",
                    logical_date=datetime(2026, 10, 1, 3, len(runs), tzinfo=UTC),
                    conf=body.get("conf") or {},
                )
                runs[run_id]["run_type"] = "manual"
                runs[run_id]["external_trigger"] = True
                return httpx.Response(200, json=runs[run_id])
        if len(s) >= 4 and s[2] == "dagRuns":
            run = self.runs[dag_id].get(s[3])
            if run is None:
                return not_found
            if len(s) == 4 and method == "GET":
                return httpx.Response(200, json=run)
            if len(s) == 5 and s[4] == "taskInstances" and method == "GET":
                tasks = self.tasks.get((dag_id, s[3]), [])
                offset = int(params.get("offset", 0))
                limit = min(int(params.get("limit", 100)), self.max_page_limit)
                return httpx.Response(
                    200,
                    json={
                        "task_instances": tasks[offset : offset + limit],
                        "total_entries": len(tasks),
                    },
                )
            if len(s) == 8 and s[4] == "taskInstances" and s[6] == "logs" and method == "GET":
                map_index = int(params.get("map_index", -1))
                text = self.logs.get((dag_id, s[3], s[5], int(s[7]), map_index))
                if text is None:
                    return not_found
                if headers.get("accept") != "text/plain":
                    # Airflow answers JSON unless plain text is asked for.
                    return httpx.Response(200, json={"content": text, "continuation_token": None})
                data = text.encode()
                size = self.log_chunk_size or max(len(data), 1)
                chunks = [data[i : i + size] for i in range(0, len(data), size)]
                return httpx.Response(
                    200, stream=_ChunkStream(chunks), headers={"Content-Type": "text/plain"}
                )
        return not_found


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.served = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.served += len(chunk)
            yield chunk


@contextmanager
def fake_airflow(scenario: FakeAirflow | None = None) -> Iterator[FakeAirflow]:
    """Route all requests to ``BASE_URL`` into the fake for the duration of the block."""
    fake = scenario or FakeAirflow()
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.route(host="airflow.test").mock(side_effect=fake.handle)
        yield fake


def canonical_scenario() -> FakeAirflow:
    """The customer_daily_load failure used across tests: a failed run with one failed task."""
    fake = FakeAirflow().add_dag("customer_daily_load")
    fake.add_run(
        "customer_daily_load",
        "scheduled__2026-09-30T02:00:00+00:00",
        state="success",
        logical_date=datetime(2026, 9, 30, 2, tzinfo=UTC),
    )
    fake.add_run(
        "customer_daily_load",
        "scheduled__2026-10-01T02:00:00+00:00",
        state="failed",
        logical_date=datetime(2026, 10, 1, 2, tzinfo=UTC),
    )
    run = "scheduled__2026-10-01T02:00:00+00:00"
    fake.add_task("customer_daily_load", run, "extract_customers", state="success")
    fake.add_task("customer_daily_load", run, "load_customers", state="failed", try_number=2)
    fake.add_task(
        "customer_daily_load", run, "dq_target_check", state="upstream_failed", try_number=0
    )
    fake.set_log(
        "customer_daily_load",
        run,
        "load_customers",
        2,
        "INFO starting load\nERROR duplicate key value violates unique constraint\n",
    )
    return fake


def make_client(**settings_overrides: Any) -> tuple[HttpAirflowClient, list[float]]:
    """A real client pointed at the fake; returns the recorded backoff sleeps too."""
    sleeps: list[float] = []

    async def record(delay: float) -> None:
        sleeps.append(delay)

    return HttpAirflowClient(fake_settings(**settings_overrides), sleep=record), sleeps
