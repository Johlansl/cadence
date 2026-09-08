"""Interactive API docs / OpenAPI schema are off in production by default,
toggled by CADENCE_API_DOCS_ENABLED (app.main.create_app)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app

DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")


def test_docs_are_404_by_default(client):
    # conftest sets no CADENCE_API_DOCS_ENABLED -> settings.api_docs_enabled False
    for path in DOCS_PATHS:
        assert client.get(path).status_code == 404, path


def test_docs_are_served_when_enabled():
    with TestClient(create_app(enable_docs=True)) as c:
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200
        assert c.get("/redoc").status_code == 200


def test_healthz_unaffected_either_way():
    with TestClient(create_app(enable_docs=True)) as c:
        assert c.get("/healthz").json() == {"status": "ok"}
