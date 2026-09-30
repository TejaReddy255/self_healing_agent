"""Audit and activity contracts (S02). ``backend.domain`` imports nothing from other backend
packages, so actor identity is carried as plain subject/role fields."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

PerformanceCounter = Literal[
    "completed_analyses", "tool_executions", "issues_detected", "total_activities"
]


class AuditEvent(BaseModel):
    """One row of ``operation_audit``: a tool call, model call, human decision or transition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    phase: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=128)
    status: str = Field(min_length=1, max_length=64)
    operation: str | None = None
    tool_name: str | None = None
    server: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    actor_subject: str | None = None
    actor_role: str | None = None
    request_id: str | None = None
    pipeline_id: str | None = None
    dag_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    analysis_id: UUID | None = None
    review_id: UUID | None = None
    job_id: UUID | None = None
    incident_id: UUID | None = None
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    details: dict[str, Any] | None = None
    occurred_at: datetime | None = None


class Activity(BaseModel):
    """A row of the operator-facing activity stream."""

    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    type: str
    details: dict[str, Any]
    incident_id: UUID | None = None
    pipeline_id: str | None = None
