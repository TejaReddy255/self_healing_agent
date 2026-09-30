from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable, Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.observability.logging import build_handler
from backend.observability.readiness import ComponentStatus, ReadinessRegistry
from tests.conftest import auth


@pytest.fixture
def log_lines() -> Iterator[Callable[[], list[dict[str, object]]]]:
    """Capture root-logger output rendered by the production JSON handler."""
    stream = io.StringIO()
    handler = build_handler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines() if line]
    finally:
        root.removeHandler(handler)


# --- S01-AC2: service info and liveness -----------------------------------------------------


@pytest.mark.spec("S01-AC2")
def test_service_info(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {
        "service": "self-healing-control-plane",
        "version": "0.1.0",
        "status": "ok",
    }


@pytest.mark.spec("S01-AC2")
def test_liveness_needs_no_auth(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.spec("S01-AC2")
def test_asgi_entrypoint_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENV", "test")
    import importlib

    import backend.main

    module = importlib.reload(backend.main)
    assert isinstance(module.app, FastAPI)


# --- S01-AC4: request correlation -------------------------------------------------------------


@pytest.mark.spec("S01-AC4")
def test_inbound_request_id_is_propagated_and_logged(
    client: TestClient, log_lines: Callable[[], list[dict[str, object]]]
) -> None:
    response = client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"
    completed = [line for line in log_lines() if line["message"] == "request completed"]
    assert completed, "expected a per-request log line"
    assert completed[-1]["request_id"] == "abc-123"
    assert completed[-1]["path"] == "/health"
    assert completed[-1]["status_code"] == 200


@pytest.mark.spec("S01-AC4")
def test_missing_request_id_is_generated(client: TestClient) -> None:
    first = client.get("/health").headers["X-Request-ID"]
    second = client.get("/health").headers["X-Request-ID"]
    assert len(first) == 32
    assert first != second


@pytest.mark.spec("S01-AC4")
@pytest.mark.parametrize("bad", ["", "has space", "x" * 200, "inject\r\nheader"])
def test_unsafe_inbound_request_id_is_replaced(client: TestClient, bad: str) -> None:
    response = client.get("/health", headers={"X-Request-ID": bad.replace("\r\n", "%0d")})
    assert response.headers["X-Request-ID"] != bad
    assert len(response.headers["X-Request-ID"]) == 32


# --- S01-AC7: CORS allowlist ----------------------------------------------------------------


@pytest.mark.spec("S01-AC7")
def test_cors_preflight(make_settings: Callable[..., Settings]) -> None:
    app = create_app(make_settings(cors_allow_origins=["https://ops.example.com"]))
    preflight = {"Access-Control-Request-Method": "GET"}
    with TestClient(app) as client:
        allowed = client.options(
            "/health", headers={"Origin": "https://ops.example.com", **preflight}
        )
        denied = client.options("/health", headers={"Origin": "https://evil.example", **preflight})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://ops.example.com"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


# --- S01-AC11: error envelope ---------------------------------------------------------------


@pytest.mark.spec("S01-AC11")
def test_unhandled_exception_returns_envelope_without_stack_trace(
    app: FastAPI, log_lines: Callable[[], list[dict[str, object]]]
) -> None:
    @app.get("/_test/boom")
    async def boom() -> None:
        raise RuntimeError("database password=hunter2 exploded")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/boom", headers={"X-Request-ID": "req-500"})

    assert response.status_code == 500
    body = response.json()
    assert body == {
        "error_code": "INTERNAL_ERROR",
        "message": "An unexpected error occurred.",
        "request_id": "req-500",
    }
    assert "Traceback" not in response.text
    assert response.headers["X-Request-ID"] == "req-500"

    errors = [line for line in log_lines() if line["message"] == "unhandled error"]
    assert errors and "Traceback" in str(errors[-1]["exc_info"])
    assert "hunter2" not in json.dumps(errors)


@pytest.mark.spec("S01-AC11")
def test_http_errors_use_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist", headers={"X-Request-ID": "req-404"})
    assert response.status_code == 404
    assert response.json() == {
        "error_code": "NOT_FOUND",
        "message": "Not Found",
        "request_id": "req-404",
    }


# --- Readiness registry (contract used by later slices) --------------------------------------


@pytest.mark.spec("S01-AC2")
def test_readiness_aggregates_component_states(app: FastAPI, client: TestClient) -> None:
    registry: ReadinessRegistry = app.state.readiness

    async def ok() -> ComponentStatus:
        return ComponentStatus(status="ok")

    async def degraded() -> ComponentStatus:
        return ComponentStatus(status="degraded", detail="replica lag")

    registry.register("db", ok)
    registry.register("airflow", degraded)
    response = client.get("/health/ready", headers=auth("viewer"))
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"

    async def broken() -> ComponentStatus:
        raise ConnectionError("postgres://u:secret@db failed")

    registry.register("mcp", broken)
    response = client.get("/health/ready", headers=auth("viewer"))
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    assert body["components"]["mcp"] == {
        "status": "down",
        "detail": "check failed: ConnectionError",
    }
    assert "secret" not in response.text


@pytest.mark.spec("S01-AC2")
async def test_readiness_check_timeout() -> None:
    registry = ReadinessRegistry(timeout_s=0.01)

    async def slow() -> ComponentStatus:
        await asyncio.sleep(1)
        return ComponentStatus(status="ok")

    registry.register("slow", slow)
    report = await registry.report()
    assert report.status == "down"
    assert report.components["slow"].detail == "check timed out"


def test_duplicate_readiness_registration_rejected() -> None:
    registry = ReadinessRegistry()

    async def ok() -> ComponentStatus:
        return ComponentStatus(status="ok")

    registry.register("db", ok)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("db", ok)
