"""Readiness registry: later slices register component checks (db, airflow, mcp, ...)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel

ComponentState = Literal["ok", "degraded", "down"]

logger = logging.getLogger(__name__)


class ComponentStatus(BaseModel):
    status: ComponentState
    detail: str | None = None


class ReadinessReport(BaseModel):
    status: ComponentState
    components: dict[str, ComponentStatus]


ReadinessCheck = Callable[[], Awaitable[ComponentStatus]]


class ReadinessRegistry:
    def __init__(self, timeout_s: float = 5.0) -> None:
        self._checks: dict[str, ReadinessCheck] = {}
        self._timeout_s = timeout_s

    def register(self, name: str, check: ReadinessCheck) -> None:
        if name in self._checks:
            raise ValueError(f"readiness check {name!r} already registered")
        self._checks[name] = check

    async def _run(self, name: str, check: ReadinessCheck) -> ComponentStatus:
        try:
            return await asyncio.wait_for(check(), timeout=self._timeout_s)
        except TimeoutError:
            return ComponentStatus(status="down", detail="check timed out")
        except Exception as exc:
            # Exception text may carry connection details; log it, expose only the type.
            logger.warning("readiness check failed", extra={"component": name}, exc_info=True)
            return ComponentStatus(status="down", detail=f"check failed: {type(exc).__name__}")

    async def report(self) -> ReadinessReport:
        names = list(self._checks)
        results = await asyncio.gather(*(self._run(n, self._checks[n]) for n in names))
        components = dict(zip(names, results, strict=True))
        states = {c.status for c in components.values()}
        overall: ComponentState = (
            "down" if "down" in states else "degraded" if "degraded" in states else "ok"
        )
        return ReadinessReport(status=overall, components=components)
