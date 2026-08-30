"""Cadence backend -- FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app import __version__
from app.api.routes import admin, fleet, hosts, jobs, reports, schedules

app = FastAPI(title="Cadence", version=__version__)

app.include_router(admin.router)
app.include_router(fleet.router)
app.include_router(hosts.router)
app.include_router(jobs.router)
app.include_router(reports.router)
app.include_router(schedules.router)
app.include_router(schedules.admin_router)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}
