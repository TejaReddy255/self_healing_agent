from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from streamlit.testing.v1 import AppTest

APP_PATH = Path(__file__).resolve().parents[3] / "frontend" / "app.py"
BASE_URL = "http://backend.test"

pytestmark = pytest.mark.spec("S01-AC8")


@pytest.fixture(scope="session", autouse=True)
def _warm_streamlit_lazy_imports() -> None:
    """Streamlit imports pandas/pyarrow lazily on the first table render. On a cold environment
    that first import (bytecode compile + antivirus scan) can exceed AppTest's per-run timeout,
    so import them once up front and keep the timeout measuring the script itself."""
    import pandas  # noqa: F401
    import pyarrow  # noqa: F401


@pytest.fixture(autouse=True)
def _frontend_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_BASE_URL", BASE_URL)
    monkeypatch.setenv("AGENT_API_TOKEN", "viewer-token")
    monkeypatch.setenv("AGENT_HEALTH_TIMEOUT_SECONDS", "2")


@pytest.fixture
def backend() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as router:
        yield router


def run_app() -> AppTest:
    app = AppTest.from_file(str(APP_PATH), default_timeout=15)
    app.run()
    assert not app.exception, app.exception
    return app


def texts(app: AppTest) -> str:
    parts = [e.value for e in (*app.success, *app.warning, *app.error, *app.caption)]
    return " | ".join(str(p) for p in parts)


def test_shows_ready_status_as_text(backend: respx.MockRouter) -> None:
    route = backend.get("/health/ready").respond(
        200, json={"status": "ok", "components": {"db": {"status": "ok", "detail": None}}}
    )
    app = run_app()
    assert "Backend status: OK" in texts(app)
    assert route.calls.last.request.headers["Authorization"] == "Bearer viewer-token"
    assert app.table, "component table expected"


def test_shows_degraded_and_down(backend: respx.MockRouter) -> None:
    backend.get("/health/ready").respond(
        503, json={"status": "down", "components": {"db": {"status": "down", "detail": "x"}}}
    )
    assert "Backend status: DOWN" in texts(run_app())


def test_backend_unreachable_is_actionable(backend: respx.MockRouter) -> None:
    backend.get("/health/ready").mock(side_effect=httpx.ConnectError("refused"))
    message = texts(run_app())
    assert "Backend unreachable" in message
    assert BASE_URL in message
    assert "Traceback" not in message


def test_backend_timeout_is_actionable(backend: respx.MockRouter) -> None:
    backend.get("/health/ready").mock(side_effect=httpx.ReadTimeout("slow"))
    assert "did not respond within 2 s" in texts(run_app())


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "AGENT_API_TOKEN"), (403, "Viewer role"), (500, "HTTP 500")],
)
def test_http_errors_are_actionable(backend: respx.MockRouter, status: int, expected: str) -> None:
    backend.get("/health/ready").respond(status, json={"error_code": "X"})
    assert expected in texts(run_app())
