"""Operator actions on Airflow (S05): registered DAGs only, every attempt audited.

The ``dag_id`` allowlist is the set of DAGs linked to ACTIVE pipelines in the current metadata
snapshot (decision 2026-10-01): the platform's Airflow credentials never reach other DAGs.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from backend.airflow.client import AirflowClient
from backend.airflow.errors import AirflowError, AirflowUnavailable
from backend.airflow.models import AirflowDag, AirflowDagRun
from backend.domain.audit import AuditEvent
from backend.metadata.service import MetadataReader
from backend.services.audit_service import AuditService

logger = logging.getLogger(__name__)


class DagNotRegistered(Exception):
    """The DAG is not linked to an active pipeline in metadata."""


@dataclass(frozen=True)
class ActorRef:
    subject: str
    role: str


def registered_dag_ids(metadata: MetadataReader) -> frozenset[str]:
    return frozenset(
        p.airflow.dag_id for p in metadata.snapshot().list_pipelines() if p.status == "ACTIVE"
    )


class AirflowActionService:
    def __init__(
        self,
        client: AirflowClient | None,
        metadata: MetadataReader,
        audit_service: AuditService | None,
    ) -> None:
        self._client = client
        self._metadata = metadata
        self._audit_service = audit_service

    def registered(self) -> frozenset[str]:
        return registered_dag_ids(self._metadata)

    def require_registered(self, dag_id: str) -> None:
        if dag_id not in self.registered():
            raise DagNotRegistered(dag_id)

    def client(self) -> AirflowClient:
        if self._client is None:
            raise AirflowUnavailable("Airflow is not configured", category="not configured")
        return self._client

    async def _require_registered_audited(
        self, event_type: str, dag_id: str, actor: ActorRef
    ) -> None:
        try:
            self.require_registered(dag_id)
        except DagNotRegistered:
            await self._audit(event_type, "DENIED", actor, dag_id, None, time.monotonic())
            raise

    async def trigger(self, dag_id: str, actor: ActorRef) -> AirflowDagRun:
        """Start a manual run with no conf (decision 2026-10-01). Never retried."""
        await self._require_registered_audited("AIRFLOW_TRIGGER", dag_id, actor)
        dag_run_id = f"manual__console__{uuid.uuid4()}"
        started = time.monotonic()
        status = "OUTCOME_UNKNOWN"  # if anything unexpected escapes, the attempt is still recorded
        try:
            run = await self.client().trigger_dag_run(dag_id, dag_run_id, conf=None)
            status = "OK"
            return run
        except AirflowError as exc:
            status = exc.category.upper()
            raise
        finally:
            await self._audit("AIRFLOW_TRIGGER", status, actor, dag_id, dag_run_id, started)

    async def set_paused(self, dag_id: str, is_paused: bool, actor: ActorRef) -> AirflowDag:
        event_type = "AIRFLOW_PAUSE" if is_paused else "AIRFLOW_RESUME"
        await self._require_registered_audited(event_type, dag_id, actor)
        started = time.monotonic()
        status = "OUTCOME_UNKNOWN"
        try:
            dag = await self.client().patch_dag(dag_id, is_paused=is_paused)
            status = "OK"
            return dag
        except AirflowError as exc:
            status = exc.category.upper()
            raise
        finally:
            await self._audit(event_type, status, actor, dag_id, None, started)

    async def _audit(
        self,
        event_type: str,
        status: str,
        actor: ActorRef,
        dag_id: str,
        run_id: str | None,
        started: float,
    ) -> None:
        if self._audit_service is None:
            logger.warning(
                "audit unavailable: airflow action not recorded (no database configured)",
                extra={"event_type": event_type, "dag_id": dag_id, "actor": actor.subject},
            )
            return
        event = AuditEvent(
            phase="airflow",
            event_type=event_type,
            status=status.replace(" ", "_"),
            operation=event_type.lower(),
            server="airflow",
            actor_subject=actor.subject,
            actor_role=actor.role,
            dag_id=dag_id,
            run_id=run_id,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            await self._audit_service.record(event)
        except Exception:
            # The Airflow action already happened. Failing the request now would make the operator
            # click again and start a second run, so surface the missing audit row loudly instead.
            logger.exception(
                "airflow action audit failed",
                extra={"event_type": event_type, "status": status, "dag_id": dag_id},
            )
