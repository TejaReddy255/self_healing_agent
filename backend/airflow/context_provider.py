"""Live Airflow context for a pipeline (src §33.8, S05).

Never raises for Airflow problems: the caller still gets static metadata plus
``unavailable=true`` and a short error category, so the operator sees what *is* known.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from backend.airflow.client import AirflowClient
from backend.airflow.errors import AirflowError, AirflowNotFound, AirflowUnavailable
from backend.airflow.models import (
    AirflowDag,
    AirflowDagRun,
    AirflowRuntimeContext,
    AirflowTaskInstance,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AirflowContextProvider:
    def __init__(
        self,
        client: AirflowClient | None,
        *,
        run_limit: int = 5,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._client = client
        self._run_limit = run_limit
        self._clock = clock

    async def get_pipeline_context(
        self, dag_id: str, run_id: str | None = None
    ) -> AirflowRuntimeContext:
        fetched_at = self._clock()
        if self._client is None:
            return AirflowRuntimeContext(
                dag_id=dag_id, fetched_at=fetched_at, unavailable=True, error="not configured"
            )
        dag: AirflowDag | None = None
        runs: list[AirflowDagRun] = []
        selected: AirflowDagRun | None = None
        tasks: list[AirflowTaskInstance] = []
        error: str | None = None
        unavailable = False
        try:
            dag = await self._client.get_dag(dag_id)
            runs = await self._client.list_dag_runs(dag_id, self._run_limit)
            if run_id is None:
                selected = runs[0] if runs else None
            else:
                selected = next((r for r in runs if r.dag_run_id == run_id), None)
                if selected is None:
                    try:
                        selected = await self._client.get_dag_run(dag_id, run_id)
                    except AirflowNotFound:
                        error = "run not found"
            if selected is not None:
                try:
                    tasks = await self._client.list_task_instances(dag_id, selected.dag_run_id)
                except AirflowNotFound:
                    error = "run not found"  # deleted between the two calls
        except AirflowNotFound:
            unavailable, error = True, "dag not found"
        except AirflowError as exc:
            unavailable, error = True, exc.category
            level = logging.WARNING if isinstance(exc, AirflowUnavailable) else logging.ERROR
            logger.log(
                level, "airflow context incomplete", extra={"dag_id": dag_id, "error": error}
            )
        return AirflowRuntimeContext(
            dag_id=dag_id,
            fetched_at=fetched_at,
            unavailable=unavailable,
            error=error,
            dag=dag,
            runs=tuple(runs),
            selected_run=selected,
            task_instances=tuple(tasks),
            failed_tasks=tuple(t for t in tasks if t.failed),
        )
