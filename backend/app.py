"""FastAPI application factory (S01). The lifespan opens no external connections yet and never
runs DDL; later slices add their resources and readiness checks here."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import health
from backend.api.errors import install_exception_handlers
from backend.config import Settings, load_settings
from backend.observability.context import REQUEST_ID_HEADER
from backend.observability.logging import configure_logging
from backend.observability.middleware import RequestContextMiddleware
from backend.observability.readiness import ReadinessRegistry
from backend.security.auth import TokenRegistry

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    token_registry = TokenRegistry.from_settings(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "service starting",
            extra={
                "env": settings.env,
                "version": settings.version,
                "configured_actors": len(token_registry),
            },
        )
        yield
        logger.info("service stopped")

    app = FastAPI(title=settings.service_name, version=settings.version, lifespan=lifespan)
    app.state.settings = settings
    app.state.token_registry = token_registry
    app.state.readiness = ReadinessRegistry()

    install_exception_handlers(app)
    app.include_router(health.router)

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
