"""Cadence backend -- FastAPI application entrypoint."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routes import admin, hosts, jobs, reports

app = FastAPI(title="Cadence", version="0.1.0")

app.include_router(admin.router)
app.include_router(hosts.router)
app.include_router(jobs.router)
app.include_router(reports.router)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}
