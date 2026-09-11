"""Cadence backend -- FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import __version__
from app.api.routes import (
    admin,
    campaigns,
    enrollments,
    exclusions,
    fleet,
    hosts,
    jobs,
    packages,
    reports,
    schedules,
    webhooks,
)
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.ratelimit import note_rejected, ratelimiter
from app.core.throttle import client_ip
from app.db.base import SessionLocal

configure_logging()
logger = logging.getLogger("cadence.request")

_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def create_app(*, enable_docs: bool | None = None) -> FastAPI:
    """Build the app. `enable_docs` overrides settings.api_docs_enabled (used by
    tests); None means read the setting. Docs are off in production by default."""
    docs = settings.api_docs_enabled if enable_docs is None else enable_docs
    app = FastAPI(
        title="Cadence",
        version=__version__,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
    )

    # Defined before log_requests so log_requests wraps it (add_middleware
    # inserts at index 0, the stack is built outermost-first): a 429 from here
    # still gets a cadence.request log line and an X-Request-ID.
    @app.middleware("http")
    async def rate_limit(request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in ("/healthz", "/readyz")
            or path == "/api/v1/agent/enroll"
            or (docs and path in _DOCS_PATHS)
            or request.headers.get("x-cadence-token-hash") is not None
            or request.headers.get("x-admin-key") is not None
        ):
            # Agent and admin traffic is metered post-auth in app.api.deps;
            # health checks and (dev-only) docs are exempt.
            return await call_next(request)
        ip = client_ip(request)
        retry_after = ratelimiter.check(
            f"dash:{ip}",
            limit=settings.ratelimit_dashboard_max,
            window=settings.ratelimit_window_seconds,
        )
        if retry_after:
            note_rejected(
                "dashboard", ip, settings.ratelimit_dashboard_max, retry_after, path
            )
            return JSONResponse(
                {"detail": "rate limit exceeded"},
                status_code=429,
                headers={"Retry-After": str(int(retry_after))},
            )
        return await call_next(request)

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request failed",
                extra={
                    "fields": {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "client": client_ip(request),
                    }
                },
            )
            raise
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "request",
            extra={
                "fields": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "client": client_ip(request),
                    # Set to "signed" by app.api.deps.get_current_host on a
                    # successful agent auth; absent for every other request.
                    "auth_scheme": getattr(request.state, "auth_scheme", None),
                }
            },
        )
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(admin.router)
    app.include_router(campaigns.router)
    app.include_router(campaigns.admin_router)
    app.include_router(enrollments.admin_router)
    app.include_router(enrollments.agent_router)
    app.include_router(exclusions.router)
    app.include_router(exclusions.admin_router)
    app.include_router(fleet.router)
    app.include_router(hosts.router)
    app.include_router(jobs.router)
    app.include_router(packages.router)
    app.include_router(reports.router)
    app.include_router(schedules.router)
    app.include_router(schedules.admin_router)
    app.include_router(webhooks.router)
    app.include_router(webhooks.admin_router)

    @app.get("/healthz", tags=["meta"])
    def healthz() -> dict[str, str]:
        """Liveness: the process is up. Does not touch the database."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["meta"])
    def readyz() -> dict[str, str]:
        """Readiness: the process can reach the database."""
        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
        finally:
            db.close()
        return {"status": "ready"}

    return app


app = create_app()
