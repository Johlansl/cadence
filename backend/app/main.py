"""Cadence backend -- FastAPI application entrypoint."""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from sqlalchemy import text

from app import __version__
from app.api.routes import admin, fleet, hosts, jobs, packages, reports, schedules
from app.core.logging import configure_logging
from app.core.throttle import client_ip
from app.db.base import SessionLocal

configure_logging()
logger = logging.getLogger("cadence.request")

app = FastAPI(title="Cadence", version=__version__)


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
                # Set by app.api.deps.get_current_host on a successful agent
                # auth ("bearer" or "signed"); absent for every other request.
                # Lets an operator grep for zero legacy-path hits before
                # retiring bearer support (docs/decisions.md "Authentication").
                "auth_scheme": getattr(request.state, "auth_scheme", None),
            }
        },
    )
    response.headers["X-Request-ID"] = request_id
    return response


app.include_router(admin.router)
app.include_router(fleet.router)
app.include_router(hosts.router)
app.include_router(jobs.router)
app.include_router(packages.router)
app.include_router(reports.router)
app.include_router(schedules.router)
app.include_router(schedules.admin_router)


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
