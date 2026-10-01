"""Recovery run trigger and terminal wait (S05, ADR-011). Worker code only.

Not exposed as an agent tool or through any route. The trigger is idempotent by a deterministic
``dag_run_id``, so a worker retry after a crash never creates a second Airflow run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.airflow.client import AirflowClient
from backend.airflow.errors import AirflowConflict, AirflowNotFound, AirflowUnavailable
from backend.airflow.models import AirflowDagRun

logger = logging.getLogger(__name__)

CONF_KEY = "self_healing"


def recovery_run_id(review_id: UUID, revision: int) -> str:
    return f"recovery__{review_id}__r{revision}"


def _is_ours(run: AirflowDagRun, review_id: UUID, revision: int) -> bool:
    marker = run.conf.get(CONF_KEY)
    return (
        isinstance(marker, dict)
        and marker.get("review_id") == str(review_id)
        and marker.get("revision") == revision
    )


async def trigger_recovery_run(
    client: AirflowClient,
    dag_id: str,
    review_id: UUID,
    revision: int,
    recovery_of_run_id: str | None,
) -> AirflowDagRun:
    """Create (or find) the recovery run for one approved revision."""
    dag_run_id = recovery_run_id(review_id, revision)
    conf = {
        CONF_KEY: {
            "review_id": str(review_id),
            "revision": revision,
            "recovery_of": recovery_of_run_id,
        }
    }
    try:
        return await client.trigger_dag_run(dag_id, dag_run_id, conf)
    except AirflowConflict:
        try:
            existing = await client.get_dag_run(dag_id, dag_run_id)
        except AirflowNotFound as exc:
            # 409 without our run: a logical-date collision. Needs an operator (never auto-retried).
            raise AirflowConflict(
                f"Airflow rejected recovery run {dag_run_id} with a conflict, but it does not exist"
            ) from exc
        if not _is_ours(existing, review_id, revision):
            raise AirflowConflict(
                f"run {dag_run_id} already exists and was not created for this review"
            ) from None
        logger.info("recovery run already exists", extra={"dag_id": dag_id, "run_id": dag_run_id})
        return existing


class RecoveryOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["success", "failed", "timeout"]
    run: AirflowDagRun | None = None
    reason: str | None = None


async def wait_for_terminal(
    client: AirflowClient,
    dag_id: str,
    dag_run_id: str,
    *,
    timeout_s: float,
    poll_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> RecoveryOutcome:
    """Poll until the run is terminal or ``timeout_s`` passes. Stateless: call again to resume."""
    deadline = clock() + timeout_s
    last: AirflowDagRun | None = None
    while True:
        try:
            last = await client.get_dag_run(dag_id, dag_run_id)
        except AirflowNotFound:
            return RecoveryOutcome(status="failed", run=last, reason="run disappeared")
        except AirflowUnavailable:
            logger.warning(
                "airflow unavailable while waiting for recovery run",
                extra={"dag_id": dag_id, "run_id": dag_run_id},
            )
        else:
            if last.state == "success":
                return RecoveryOutcome(status="success", run=last)
            if last.state == "failed":
                return RecoveryOutcome(status="failed", run=last, reason="run failed")
        remaining = deadline - clock()
        if remaining <= 0:
            return RecoveryOutcome(status="timeout", run=last, reason="no terminal state in time")
        await sleep(min(poll_s, remaining))
