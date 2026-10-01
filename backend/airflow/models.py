"""Airflow 2.x REST shapes used by the platform (S05).

Models ignore unknown fields and keep states as strings, so a newer Airflow release never breaks
parsing. Datetimes are normalised to UTC.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.common import JsonObject, UtcDatetime

# Known states (Airflow 2.x). Anything else is passed through as-is.
DAG_RUN_TERMINAL = frozenset({"success", "failed"})
FAILED_TASK_STATES = frozenset({"failed", "upstream_failed"})


class AirflowModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class AirflowDag(AirflowModel):
    dag_id: str
    is_paused: bool | None = None
    is_active: bool | None = None
    # A cron/timedelta object ({"__type": ..., "value": ...}), a preset string, or null.
    schedule_interval: JsonObject | str | None = None
    timetable_description: str | None = None
    next_dagrun: UtcDatetime | None = None
    next_dagrun_data_interval_start: UtcDatetime | None = None
    next_dagrun_data_interval_end: UtcDatetime | None = None
    tags: tuple[str, ...] = ()

    @field_validator("tags", mode="before")
    @classmethod
    def _tag_names(cls, value: Any) -> Any:
        # Airflow returns tags as [{"name": "x"}].
        if isinstance(value, list):
            return tuple(t.get("name", "") if isinstance(t, dict) else str(t) for t in value)
        return value if value is not None else ()


class AirflowDagRun(AirflowModel):
    dag_id: str
    dag_run_id: str
    state: str | None = None
    run_type: str | None = None
    logical_date: UtcDatetime | None = None
    data_interval_start: UtcDatetime | None = None
    data_interval_end: UtcDatetime | None = None
    start_date: UtcDatetime | None = None
    end_date: UtcDatetime | None = None
    external_trigger: bool | None = None
    conf: dict[str, Any] = Field(default_factory=dict)

    @field_validator("conf", mode="before")
    @classmethod
    def _null_conf(cls, value: Any) -> Any:
        return {} if value is None else value

    @property
    def terminal(self) -> bool:
        return self.state in DAG_RUN_TERMINAL


class AirflowTaskInstance(AirflowModel):
    task_id: str
    map_index: int = -1
    state: str | None = None
    try_number: int = 0
    start_date: UtcDatetime | None = None
    end_date: UtcDatetime | None = None
    duration: float | None = None
    operator: str | None = None

    @property
    def failed(self) -> bool:
        return self.state in FAILED_TASK_STATES


class AirflowHealth(AirflowModel):
    metadatabase: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    scheduler: Literal["healthy", "unhealthy", "unknown"] = "unknown"
    latest_scheduler_heartbeat: UtcDatetime | None = None

    @property
    def healthy(self) -> bool:
        return self.metadatabase == "healthy" and self.scheduler == "healthy"


class RawLog(AirflowModel):
    """The last bytes of a task log (a rolling tail); ``download_capped`` means the download stopped
    at the ceiling, so ``text`` ends earlier than the real log."""

    text: str
    downloaded_bytes: int
    download_capped: bool


class TaskLog(AirflowModel):
    dag_id: str
    dag_run_id: str
    task_id: str
    try_number: int
    map_index: int = -1
    content: str
    truncated: bool
    original_chars: int


class DagListing(AirflowModel):
    items: tuple[AirflowDag, ...]
    missing: tuple[str, ...] = ()


class AirflowRuntimeContext(AirflowModel):
    """Live Airflow state for one pipeline. Never stored: read fresh every time (invariant 1)."""

    dag_id: str
    source: Literal["airflow"] = "airflow"
    fetched_at: datetime
    unavailable: bool = False
    error: str | None = None
    dag: AirflowDag | None = None
    runs: tuple[AirflowDagRun, ...] = ()
    selected_run: AirflowDagRun | None = None
    task_instances: tuple[AirflowTaskInstance, ...] = ()
    failed_tasks: tuple[AirflowTaskInstance, ...] = ()
