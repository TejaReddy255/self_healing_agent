"""Service info, liveness and readiness routes (S01)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.observability.readiness import ReadinessRegistry, ReadinessReport
from backend.security.auth import Actor, Role, require_role

router = APIRouter()


class ServiceInfo(BaseModel):
    service: str
    version: str
    status: str


class Liveness(BaseModel):
    status: str


@router.get("/", response_model=ServiceInfo)
async def service_info(request: Request) -> ServiceInfo:
    settings = request.app.state.settings
    return ServiceInfo(service=settings.service_name, version=settings.version, status="ok")


@router.get("/health", response_model=Liveness)
async def liveness() -> Liveness:
    return Liveness(status="ok")


@router.get("/health/ready", response_model=ReadinessReport)
async def readiness(
    request: Request, _: Annotated[Actor, Depends(require_role(Role.VIEWER))]
) -> JSONResponse:
    registry: ReadinessRegistry = request.app.state.readiness
    report = await registry.report()
    return JSONResponse(
        status_code=503 if report.status == "down" else 200,
        content=report.model_dump(mode="json"),
    )
