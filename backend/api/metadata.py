"""Static metadata API (S04, architecture §6). Read routes are viewer; reload is admin-only."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from backend.api.errors import ApiError
from backend.metadata.models import (
    BusinessRuleSet,
    KnownIssue,
    MetadataHealth,
    Ownership,
    PipelineMetadata,
    ReloadResult,
    Runbook,
    SOPMetadata,
)
from backend.metadata.repository import MetadataSnapshot
from backend.metadata.service import MetadataService
from backend.security.auth import Actor, Role, require_role

router = APIRouter(prefix="/metadata", tags=["metadata"])

Viewer = Annotated[Actor, Depends(require_role(Role.VIEWER))]
Admin = Annotated[Actor, Depends(require_role(Role.ADMIN))]
PipelineFilter = Annotated[str | None, Query(max_length=128)]
CategoryFilter = Annotated[str | None, Query(max_length=64)]


class ItemList[T](BaseModel):
    """A list from one snapshot, tagged with that snapshot's version."""

    version: str | None
    items: list[T]


def get_metadata_service(request: Request) -> MetadataService:
    service: MetadataService = request.app.state.metadata_service
    return service


Service = Annotated[MetadataService, Depends(get_metadata_service)]


def _require_pipeline(snap: MetadataSnapshot, pipeline_id: str | None) -> None:
    if pipeline_id is not None and snap.get_pipeline(pipeline_id) is None:
        raise ApiError(404, "METADATA_NOT_FOUND", "Unknown pipeline.")


@router.get("/health", response_model=MetadataHealth)
async def metadata_health(service: Service, _: Viewer) -> MetadataHealth:
    return service.health()


@router.post("/reload", response_model=ReloadResult)
async def reload_metadata(service: Service, actor: Admin) -> ReloadResult:
    result = await service.reload(actor.subject, actor.role.value)
    if result.outcome == "REJECTED":
        raise ApiError(
            422,
            "METADATA_INVALID",
            f"Metadata reload rejected with {len(result.errors)} error(s); "
            "the previous metadata is still active.",
            details={
                "active_version": result.previous_version,
                "candidate_version": result.version,
                "errors": [e.model_dump(mode="json") for e in result.errors],
                "warnings": [w.model_dump(mode="json") for w in result.warnings],
            },
        )
    return result


@router.get("/pipelines", response_model=ItemList[PipelineMetadata])
async def list_pipelines(service: Service, _: Viewer) -> ItemList[PipelineMetadata]:
    snap = service.snapshot()
    return ItemList(version=snap.version, items=list(snap.list_pipelines()))


@router.get("/pipelines/{pipeline_id}", response_model=PipelineMetadata)
async def get_pipeline(pipeline_id: str, service: Service, _: Viewer) -> PipelineMetadata:
    pipeline = service.snapshot().get_pipeline(pipeline_id)
    if pipeline is None:
        raise ApiError(404, "METADATA_NOT_FOUND", "Unknown pipeline.")
    return pipeline


@router.get("/sops", response_model=ItemList[SOPMetadata])
async def list_sops(
    service: Service,
    _: Viewer,
    pipeline_id: PipelineFilter = None,
    category: CategoryFilter = None,
    status: Literal["APPROVED", "DRAFT", "DEPRECATED"] | None = "APPROVED",
) -> ItemList[SOPMetadata]:
    snap = service.snapshot()
    _require_pipeline(snap, pipeline_id)
    candidates = (
        snap.all_sops()
        if pipeline_id is None
        else snap.sops(pipeline_id, category, approved_only=False)
    )
    items = [
        s
        for s in candidates
        if (category is None or s.category == category) and (status is None or s.status == status)
    ]
    return ItemList(version=snap.version, items=items)


@router.get("/known-issues", response_model=ItemList[KnownIssue])
async def list_known_issues(
    service: Service, _: Viewer, pipeline_id: PipelineFilter = None, category: CategoryFilter = None
) -> ItemList[KnownIssue]:
    snap = service.snapshot()
    _require_pipeline(snap, pipeline_id)
    items = snap.all_known_issues() if pipeline_id is None else snap.known_issues(pipeline_id)
    return ItemList(
        version=snap.version,
        items=[k for k in items if category is None or k.category == category],
    )


@router.get("/business-rules", response_model=ItemList[BusinessRuleSet])
async def list_business_rules(
    service: Service, _: Viewer, pipeline_id: PipelineFilter = None
) -> ItemList[BusinessRuleSet]:
    snap = service.snapshot()
    _require_pipeline(snap, pipeline_id)
    items = snap.all_business_rules() if pipeline_id is None else snap.business_rules(pipeline_id)
    return ItemList(version=snap.version, items=list(items))


@router.get("/runbooks", response_model=ItemList[Runbook])
async def list_runbooks(
    service: Service, _: Viewer, pipeline_id: PipelineFilter = None, category: CategoryFilter = None
) -> ItemList[Runbook]:
    snap = service.snapshot()
    _require_pipeline(snap, pipeline_id)
    items = snap.all_runbooks() if pipeline_id is None else snap.runbooks(pipeline_id)
    return ItemList(
        version=snap.version,
        items=[r for r in items if category is None or category in r.categories],
    )


@router.get("/ownership", response_model=ItemList[Ownership])
async def list_ownership(
    service: Service, _: Viewer, pipeline_id: PipelineFilter = None
) -> ItemList[Ownership]:
    snap = service.snapshot()
    _require_pipeline(snap, pipeline_id)
    if pipeline_id is None:
        items = list(snap.all_ownership())
    else:
        owner = snap.ownership(pipeline_id)
        items = [owner] if owner is not None else []
    return ItemList(version=snap.version, items=items)
