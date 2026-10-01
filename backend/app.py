"""FastAPI application factory.

The lifespan opens and closes the database pool and **never runs DDL** (ADR-003): schema changes
happen only through ``python -m backend.db.migrate``; readiness reports pending migrations.
Static metadata is loaded once at startup (an invalid tree leaves it EMPTY rather than failing,
S04) and refreshed in the background while the app runs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api import activities, health, metadata
from backend.api.errors import error_response, install_exception_handlers
from backend.config import ConfigurationError, Settings, load_settings
from backend.db.health import migrations_check
from backend.db.migrations import MigrationError, discover
from backend.db.pool import Database, DatabaseUnavailableError
from backend.db.settings import DatabaseSettings, load_database_settings
from backend.metadata.refresher import MetadataRefresher
from backend.metadata.service import MetadataService
from backend.metadata.settings import MetadataSettings, load_metadata_settings
from backend.observability.context import REQUEST_ID_HEADER
from backend.observability.logging import configure_logging
from backend.observability.middleware import RequestContextMiddleware
from backend.observability.readiness import ComponentStatus, ReadinessRegistry
from backend.security.auth import TokenRegistry
from backend.services.activity_service import ActivityBroadcaster, ActivityService
from backend.services.audit_service import AuditService
from backend.services.metadata_audit import make_reload_auditor

logger = logging.getLogger(__name__)


async def _database_unavailable(_: Request, exc: Exception) -> JSONResponse:
    # Expected operational condition (DB down/restarting): no stack trace per request.
    logger.warning("database unavailable", extra={"reason": str(exc)})
    return error_response(503, "DATABASE_UNAVAILABLE", "The database is currently unavailable.")


def create_app(
    settings: Settings | None = None,
    db_settings: DatabaseSettings | None = None,
    metadata_settings: MetadataSettings | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    db_settings = db_settings or load_database_settings()
    metadata_settings = metadata_settings or load_metadata_settings()
    configure_logging(settings.log_level)
    token_registry = TokenRegistry.from_settings(settings)

    if settings.env == "prod" and len(token_registry) == 0:
        logger.warning("no API tokens configured: every authenticated route will return 401")
    if settings.env == "prod" and not db_settings.configured:
        raise ConfigurationError("ENV=prod requires the operational database: set PG_PASSWORD")

    readiness = ReadinessRegistry()
    broadcaster = ActivityBroadcaster()
    database: Database | None = None
    audit_service: AuditService | None = None
    activity_service: ActivityService | None = None
    if db_settings.configured:
        try:
            migration_files = discover(db_settings.migrations_dir)
        except MigrationError as exc:
            raise ConfigurationError(f"invalid migrations directory: {exc}") from exc
        database = Database(db_settings, application_name=settings.service_name)
        audit_service = AuditService(database, settings.audit_max_payload_bytes)
        activity_service = ActivityService(database, broadcaster, settings.audit_max_payload_bytes)
        readiness.register("db", database.check)
        readiness.register("migrations", migrations_check(database, migration_files))
    else:
        logger.warning("database not configured (PG_PASSWORD unset): persistence is disabled")

    metadata_service = MetadataService(
        metadata_settings, auditor=make_reload_auditor(audit_service)
    )
    metadata_service.load_initial()

    async def metadata_readiness() -> ComponentStatus:
        health = metadata_service.health()
        if health.state == "EMPTY":
            return ComponentStatus(status="down", detail=f"{len(health.errors)} errors")
        if health.rejected_candidate is not None:
            rejected = health.rejected_candidate
            return ComponentStatus(
                status="degraded",
                detail=f"candidate {rejected.version[:12]} rejected: {len(rejected.errors)} errors",
            )
        return ComponentStatus(status="ok")

    readiness.register("metadata", metadata_readiness)
    metadata_refresher = MetadataRefresher(
        metadata_service, metadata_settings.metadata_refresh_interval_s
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service starting",
            extra={
                "env": settings.env,
                "version": settings.version,
                "configured_actors": len(token_registry),
                "database": db_settings.describe() if database else None,
            },
        )
        if database is not None:
            await database.open()
        metadata_refresher.start()
        try:
            yield
        finally:
            await metadata_refresher.stop()
            if database is not None:
                await database.close()
            logger.info("service stopped")

    # Interactive docs and the OpenAPI schema are unauthenticated, so they are off in prod.
    docs_enabled = settings.env != "prod"
    app = FastAPI(
        title=settings.service_name,
        version=settings.version,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
    )
    app.state.settings = settings
    app.state.token_registry = token_registry
    app.state.readiness = readiness
    app.state.database = database
    app.state.broadcaster = broadcaster
    app.state.audit_service = audit_service
    app.state.activity_service = activity_service
    app.state.metadata_service = metadata_service
    app.state.metadata_refresher = metadata_refresher

    install_exception_handlers(app)
    app.add_exception_handler(DatabaseUnavailableError, _database_unavailable)
    app.include_router(health.router)
    app.include_router(activities.router)
    app.include_router(metadata.router)

    # Added last = outermost: CORS preflight is answered before request handling.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    return app
