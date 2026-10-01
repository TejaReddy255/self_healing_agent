"""Airflow proxy API for the operator console (S05, ADR-009). Registered DAGs only."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.airflow.errors import (
    AirflowAuthError,
    AirflowConflict,
    AirflowError,
    AirflowNotFound,
    AirflowUnavailable,
)
from backend.airflow.models import AirflowDag, AirflowDagRun, AirflowTaskInstance, DagListing
from backend.api.errors import error_response
from backend.security.auth import Actor, Role, require_role
from backend.services.airflow_actions import ActorRef, AirflowActionService, DagNotRegistered

router = APIRouter(prefix="/airflow", tags=["airflow"])

Viewer = Annotated[Actor, Depends(require_role(Role.VIEWER))]
Operator = Annotated[Actor, Depends(require_role(Role.OPERATOR))]
DagIdPath = Annotated[str, Path(min_length=1, max_length=250)]
RunIdPath = Annotated[str, Path(min_length=1, max_length=250)]


class ItemList[T](BaseModel):
    items: list[T]


class DagList(ItemList[AirflowDag]):
    missing: list[str] = []


class TriggerRequest(BaseModel):
    """No ``conf`` in v1 (decision 2026-10-01): unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_paused: bool


def get_actions(request: Request) -> AirflowActionService:
    service: AirflowActionService = request.app.state.airflow_actions
    return service


Actions = Annotated[AirflowActionService, Depends(get_actions)]


@router.get("/dags", response_model=DagList)
async def list_dags(actions: Actions, _: Viewer) -> DagList:
    listing: DagListing = await actions.client().list_dags(sorted(actions.registered()))
    return DagList(items=list(listing.items), missing=list(listing.missing))


@router.get("/dags/{dag_id}/runs", response_model=ItemList[AirflowDagRun])
async def list_runs(
    dag_id: DagIdPath,
    actions: Actions,
    _: Viewer,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> ItemList[AirflowDagRun]:
    actions.require_registered(dag_id)
    return ItemList(items=await actions.client().list_dag_runs(dag_id, limit))


@router.get("/dags/{dag_id}/runs/{run_id}/tasks", response_model=ItemList[AirflowTaskInstance])
async def list_tasks(
    dag_id: DagIdPath, run_id: RunIdPath, actions: Actions, _: Viewer
) -> ItemList[AirflowTaskInstance]:
    actions.require_registered(dag_id)
    return ItemList(items=await actions.client().list_task_instances(dag_id, run_id))


@router.post("/dags/{dag_id}/runs", response_model=AirflowDagRun, status_code=201)
async def trigger_run(
    dag_id: DagIdPath, body: TriggerRequest, actions: Actions, actor: Operator
) -> AirflowDagRun:
    return await actions.trigger(dag_id, ActorRef(actor.subject, actor.role.value))


@router.patch("/dags/{dag_id}", response_model=AirflowDag)
async def set_paused(
    dag_id: DagIdPath, body: PauseRequest, actions: Actions, actor: Operator
) -> AirflowDag:
    return await actions.set_paused(
        dag_id, body.is_paused, ActorRef(actor.subject, actor.role.value)
    )


# --- error mapping ---------------------------------------------------------------------------


async def _dag_not_registered(_: Request, exc: Exception) -> JSONResponse:
    return error_response(
        404, "AIRFLOW_DAG_NOT_REGISTERED", "This DAG is not linked to an active pipeline."
    )


async def _airflow_error(_: Request, exc: Exception) -> JSONResponse:
    # Messages are generic: Airflow response bodies are never echoed to callers.
    if isinstance(exc, AirflowUnavailable):
        return error_response(503, "AIRFLOW_UNAVAILABLE", "Airflow is currently unavailable.")
    if isinstance(exc, AirflowNotFound):
        return error_response(404, "AIRFLOW_NOT_FOUND", "Airflow has no such resource.")
    if isinstance(exc, AirflowConflict):
        return error_response(409, "AIRFLOW_CONFLICT", "Airflow reported a conflict.")
    if isinstance(exc, AirflowAuthError):
        return error_response(
            502, "AIRFLOW_AUTH_FAILED", "The platform could not authenticate to Airflow."
        )
    return error_response(502, "AIRFLOW_BAD_RESPONSE", "Airflow returned an unexpected response.")


def install_airflow_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DagNotRegistered, _dag_not_registered)
    app.add_exception_handler(AirflowError, _airflow_error)
