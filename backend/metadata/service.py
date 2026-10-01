"""MetadataService: typed, read-only access to the active snapshot, plus atomic reload (S04).

Consumers never read metadata files (FR-004). Callers that need several results from *one*
consistent view take ``snapshot()`` once and query it; the async convenience methods each read
the snapshot that is active at the time of the call.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

from backend.metadata.models import (
    BusinessRuleSet,
    HistoricalIncident,
    KnownIssue,
    MetadataDocument,
    MetadataHealth,
    MetadataQuery,
    Ownership,
    PipelineMetadata,
    RejectedCandidate,
    ReloadResult,
    Runbook,
    SOPMetadata,
)
from backend.metadata.repository import MetadataSnapshot
from backend.metadata.settings import MetadataSettings
from backend.metadata.validators import Candidate, build_candidate, current_version
from backend.observability.readiness import ComponentStatus

logger = logging.getLogger(__name__)

SYSTEM_SUBJECT = "system:metadata-refresher"
SYSTEM_ROLE = "service"


class ReloadAuditor(Protocol):
    async def __call__(self, result: ReloadResult, subject: str, role: str) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MetadataService:
    def __init__(
        self,
        settings: MetadataSettings,
        *,
        auditor: ReloadAuditor | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._root = settings.resolved_dir()
        self._max_bytes = settings.metadata_max_file_bytes
        self._auditor = auditor
        self._clock = clock
        self._lock = asyncio.Lock()
        self._snapshot = MetadataSnapshot.empty(clock())
        self._rejected: RejectedCandidate | None = None
        self._last_rejected_version: str | None = None

    @property
    def root(self) -> str:
        return str(self._root)

    def _build(self) -> Candidate:
        return build_candidate(self._root, self._max_bytes, self._clock())

    def load_initial(self) -> MetadataHealth:
        """Synchronous first load at process start. An invalid tree leaves the service EMPTY
        (decision 2026-10-01): the process still starts and a later reload can fill it."""
        candidate = self._build()
        if candidate.snapshot is not None:
            self._snapshot = candidate.snapshot
        else:
            self._snapshot = MetadataSnapshot.empty(
                self._clock(), candidate.errors, candidate.warnings
            )
            self._last_rejected_version = candidate.version
            logger.error(
                "metadata invalid at startup; serving no metadata until it is fixed",
                extra={"metadata_dir": self.root, "error_count": len(candidate.errors)},
            )
        health = self.health()
        logger.info(
            "metadata loaded",
            extra={
                "metadata_dir": self.root,
                "metadata_state": health.state,
                "metadata_version": health.version,
                "warning_count": len(health.warnings),
            },
        )
        return health

    # --- state ------------------------------------------------------------------------------

    def snapshot(self) -> MetadataSnapshot:
        return self._snapshot

    @property
    def state(self) -> Literal["LOADED", "DEGRADED", "EMPTY"]:
        if self._snapshot.is_empty:
            return "EMPTY"
        return "DEGRADED" if self._rejected is not None else "LOADED"

    def health(self) -> MetadataHealth:
        snap = self._snapshot
        return MetadataHealth(
            state=self.state,
            version=snap.version,
            loaded_at=snap.loaded_at,
            counts_by_type=snap.counts_by_type(),
            errors=snap.errors,
            warnings=snap.warnings,
            rejected_candidate=self._rejected,
        )

    async def readiness(self) -> ComponentStatus:
        state = self.state
        if state == "EMPTY":
            return ComponentStatus(status="down", detail=f"{len(self._snapshot.errors)} errors")
        if state == "DEGRADED" and self._rejected is not None:
            return ComponentStatus(
                status="degraded",
                detail=f"candidate {self._rejected.version[:12]} rejected: "
                f"{len(self._rejected.errors)} errors",
            )
        return ComponentStatus(status="ok")

    # --- reload -----------------------------------------------------------------------------

    async def reload(self, subject: str = SYSTEM_SUBJECT, role: str = SYSTEM_ROLE) -> ReloadResult:
        """Validate the tree on disk and swap it in only if it has no errors (atomic)."""
        async with self._lock:
            candidate = await asyncio.to_thread(self._build)
            previous = self._snapshot.version
            if candidate.snapshot is not None and candidate.version == previous:
                self._rejected = None
                result = self._result("UNCHANGED", previous, candidate)
            elif candidate.snapshot is None:
                self._last_rejected_version = candidate.version
                if self._snapshot.is_empty:
                    self._snapshot = MetadataSnapshot.empty(
                        self._clock(), candidate.errors, candidate.warnings
                    )
                else:
                    self._rejected = RejectedCandidate(
                        version=candidate.version,
                        checked_at=self._clock(),
                        errors=candidate.errors,
                        warnings=candidate.warnings,
                    )
                result = self._result("REJECTED", previous, candidate)
                logger.warning(
                    "metadata reload rejected; keeping last-known-good",
                    extra={
                        "candidate_version": candidate.version,
                        "active_version": previous,
                        "error_count": len(candidate.errors),
                    },
                )
            else:
                self._snapshot = candidate.snapshot  # single reference swap
                self._rejected = None
                self._last_rejected_version = None
                result = self._result("RELOADED", previous, candidate)
                logger.info(
                    "metadata reloaded",
                    extra={"previous_version": previous, "metadata_version": candidate.version},
                )
        await self._audit(result, subject, role)
        return result

    async def refresh_if_changed(self) -> ReloadResult | None:
        """Reload when the tree on disk changed since the last swap or rejection."""
        version = await asyncio.to_thread(current_version, self._root, self._max_bytes)
        if version == self._snapshot.version:
            self._rejected = None  # the files were restored to the active tree
            return None
        if version == self._last_rejected_version:
            return None  # already reported; wait for the next change
        return await self.reload(SYSTEM_SUBJECT, SYSTEM_ROLE)

    def _result(
        self,
        outcome: Literal["RELOADED", "UNCHANGED", "REJECTED"],
        previous: str | None,
        candidate: Candidate,
    ) -> ReloadResult:
        return ReloadResult(
            outcome=outcome,
            previous_version=previous,
            version=candidate.version,
            errors=candidate.errors,
            warnings=candidate.warnings,
            health=self.health(),
        )

    async def _audit(self, result: ReloadResult, subject: str, role: str) -> None:
        if self._auditor is None:
            return
        try:
            await self._auditor(result, subject, role)
        except Exception:
            # The swap already happened (files cannot join a DB transaction): never hide it,
            # but surface the missing audit row loudly.
            logger.exception(
                "metadata reload audit failed",
                extra={"outcome": result.outcome, "metadata_version": result.version},
            )

    # --- typed queries (each reads the snapshot active at call time) -------------------------

    async def get_pipeline(self, pipeline_id: str) -> PipelineMetadata | None:
        return self._snapshot.get_pipeline(pipeline_id)

    async def get_pipeline_by_dag_id(self, dag_id: str) -> PipelineMetadata | None:
        return self._snapshot.get_pipeline_by_dag_id(dag_id)

    async def list_pipelines(self) -> tuple[PipelineMetadata, ...]:
        return self._snapshot.list_pipelines()

    async def get_business_rules(self, pipeline_id: str) -> tuple[BusinessRuleSet, ...]:
        return self._snapshot.business_rules(pipeline_id)

    async def get_sops(
        self, pipeline_id: str, category: str | None = None
    ) -> tuple[SOPMetadata, ...]:
        return self._snapshot.sops(pipeline_id, category)

    async def get_sop(self, sop_id: str) -> SOPMetadata | None:
        return self._snapshot.get_sop(sop_id)

    async def get_runbooks(
        self, pipeline_id: str, category: str | None = None
    ) -> tuple[Runbook, ...]:
        return self._snapshot.runbooks(pipeline_id, category)

    async def get_known_issues(
        self, pipeline_id: str, category: str | None = None
    ) -> tuple[KnownIssue, ...]:
        return self._snapshot.known_issues(pipeline_id, category)

    async def get_historical_incidents(
        self, pipeline_id: str, category: str | None = None
    ) -> tuple[HistoricalIncident, ...]:
        return self._snapshot.historical_incidents(pipeline_id, category)

    async def get_ownership(self, pipeline_id: str) -> Ownership | None:
        return self._snapshot.ownership(pipeline_id)

    async def search(self, query: MetadataQuery) -> tuple[MetadataDocument, ...]:
        return self._snapshot.search(query)
