"""Combined pipeline context (S05, architecture §18 C5): static intent plus live Airflow state.

The two sections stay separate references, so authority is never blurred (src §35.3): the
``dag_id`` comes only from metadata, and Airflow's live state is reported as-is with its source
and fetch time.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from backend.airflow.context_provider import AirflowContextProvider
from backend.airflow.models import AirflowRuntimeContext
from backend.api.errors import ApiError
from backend.metadata.models import PipelineMetadata
from backend.metadata.service import MetadataReader
from backend.security.auth import Actor, Role, require_role

router = APIRouter(tags=["pipelines"])


class PipelineContext(BaseModel):
    pipeline_id: str
    metadata_version: str | None
    static_metadata: PipelineMetadata
    airflow: AirflowRuntimeContext


@router.get("/pipelines/{pipeline_id}/context", response_model=PipelineContext)
async def pipeline_context(
    pipeline_id: str,
    request: Request,
    _: Annotated[Actor, Depends(require_role(Role.VIEWER))],
    run_id: Annotated[str | None, Query(min_length=1, max_length=250)] = None,
) -> PipelineContext:
    metadata: MetadataReader = request.app.state.metadata_service
    provider: AirflowContextProvider = request.app.state.airflow_context
    snap = metadata.snapshot()
    pipeline = snap.get_pipeline(pipeline_id)
    if pipeline is None:
        raise ApiError(404, "METADATA_NOT_FOUND", "Unknown pipeline.")
    airflow = await provider.get_pipeline_context(pipeline.airflow.dag_id, run_id)
    return PipelineContext(
        pipeline_id=pipeline_id,
        metadata_version=snap.version,
        static_metadata=pipeline,
        airflow=airflow,
    )
