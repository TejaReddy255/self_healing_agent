"""Read-only Airflow functions for agents (S05; wrapped as agent tools in S08).

Every function checks ``dag_id`` against the allowlist fixed at construction (the pipeline's
``airflow.dag_id``) before any HTTP call. There is deliberately no trigger or pause here: recovery
is worker code (ADR-011) and operator actions go through the API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from backend.airflow.client import AirflowReader
from backend.airflow.errors import AirflowNotFound, AirflowToolDenied
from backend.airflow.logs import tail_and_redact
from backend.airflow.models import AirflowDagRun, AirflowTaskInstance, TaskLog


class RunDetails(BaseModel):
    model_config = ConfigDict(frozen=True)

    run: AirflowDagRun
    task_states: dict[str, int]
    task_count: int


class AirflowReadTools:
    def __init__(
        self, client: AirflowReader, allowed_dag_ids: Iterable[str], *, log_max_chars: int
    ) -> None:
        self._client = client
        self._allowed = frozenset(allowed_dag_ids)
        self._log_max_chars = log_max_chars

    @property
    def allowed_dag_ids(self) -> frozenset[str]:
        return self._allowed

    def _check(self, dag_id: str) -> None:
        if dag_id not in self._allowed:
            raise AirflowToolDenied(f"DAG {dag_id!r} is not in this agent's scope")

    async def get_workflow_run_details(self, dag_id: str, run_id: str) -> RunDetails:
        self._check(dag_id)
        run = await self._client.get_dag_run(dag_id, run_id)
        tasks = await self._client.list_task_instances(dag_id, run_id)
        states: dict[str, int] = {}
        for task in tasks:
            key = task.state or "none"
            states[key] = states.get(key, 0) + 1
        return RunDetails(run=run, task_states=dict(sorted(states.items())), task_count=len(tasks))

    async def get_failed_tasks_in_run(
        self, dag_id: str, run_id: str
    ) -> tuple[AirflowTaskInstance, ...]:
        self._check(dag_id)
        tasks = await self._client.list_task_instances(dag_id, run_id)
        return tuple(t for t in tasks if t.failed)

    async def get_task_logs(
        self,
        dag_id: str,
        run_id: str,
        task_id: str,
        try_number: int | None = None,
        map_index: int | None = None,
    ) -> TaskLog:
        """The redacted tail of a task log.

        Without ``try_number`` it picks the task's latest try, preferring a failed instance (for
        mapped tasks, the failed map index). ``map_index`` selects a mapped instance explicitly.
        """
        self._check(dag_id)
        if try_number is None or map_index is None:
            tasks = [
                t
                for t in await self._client.list_task_instances(dag_id, run_id)
                if t.task_id == task_id and (map_index is None or t.map_index == map_index)
            ]
            if not tasks:
                raise AirflowNotFound(f"task {task_id!r} is not part of run {run_id!r}")
            chosen = max(tasks, key=lambda t: (t.failed, t.try_number, -t.map_index))
            if try_number is None:
                try_number = max(chosen.try_number, 1)
            if map_index is None:
                map_index = chosen.map_index
        raw = await self._client.get_task_log(dag_id, run_id, task_id, try_number, map_index)
        # Redaction is CPU work on untrusted text: keep it off the event loop.
        content, truncated, original = await asyncio.to_thread(
            tail_and_redact, raw, self._log_max_chars
        )
        return TaskLog(
            dag_id=dag_id,
            dag_run_id=run_id,
            task_id=task_id,
            try_number=try_number,
            map_index=map_index,
            content=content,
            truncated=truncated,
            original_chars=original,
        )
