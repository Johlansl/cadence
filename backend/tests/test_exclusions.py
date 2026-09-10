"""Package exclusion (hold) rule admin CRUD."""

from __future__ import annotations

import uuid

from app.models.models import AuditLog, PackageExclusion
from tests.conftest import ADMIN_HEADERS, create_host


def _create(client, **over):
    body = {"scope": "global", "pattern": "linux-image*"}
    body.update(over)
    return client.post("/api/v1/admin/package-exclusions", headers=ADMIN_HEADERS, json=body)


def test_create_global(client):
    r = _create(client, description="never touch kernels")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "global"
    assert body["host_id"] is None
    assert body["pattern"] == "linux-image*"
    assert body["description"] == "never touch kernels"
    assert "id" in body and "created_at" in body


def test_create_host_scoped(client):
    host_id, _ = create_host(client)
    r = _create(client, scope="host", host_id=host_id, pattern="postgresql-14")
    assert r.status_code == 201, r.text
    assert r.json()["host_id"] == host_id


def test_create_tag_scoped(client):
    r = _create(client, scope="tag", tag="role=web", pattern="nginx*")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scope"] == "tag"
    assert body["tag"] == "role=web"
    assert body["host_id"] is None


def test_create_tag_scoped_lowercases_the_tag(client):
    r = _create(client, scope="tag", tag="Role=Web", pattern="nginx*")
    assert r.status_code == 201, r.text
    assert r.json()["tag"] == "role=web"


def test_create_validation(client):
    host_id, _ = create_host(client)
    # host scope needs host_id
    assert _create(client, scope="host").status_code == 422
    # global scope must not carry host_id
    assert _create(client, scope="global", host_id=host_id).status_code == 422
    # host_id must reference a real host
    assert _create(client, scope="host", host_id=str(uuid.uuid4())).status_code == 404
    # tag scope needs a tag, and only a tag
    assert _create(client, scope="tag").status_code == 422
    assert _create(client, scope="tag", tag="role=web", host_id=host_id).status_code == 422
    assert _create(client, scope="tag", tag="").status_code == 422
    assert _create(client, scope="tag", tag="   ").status_code == 422
    assert _create(client, scope="tag", tag="role=").status_code == 422
    assert _create(client, scope="tag", tag="=web").status_code == 422
    # global / host scope must not carry a tag
    assert _create(client, scope="global", tag="role=web").status_code == 422
    assert _create(
        client, scope="host", host_id=host_id, tag="role=web"
    ).status_code == 422
    # pattern must look like a package name / glob
    assert _create(client, pattern="").status_code == 422
    assert _create(client, pattern="rm -rf /").status_code == 422
    assert _create(client, pattern="$(id)").status_code == 422
    # auth
    assert client.post(
        "/api/v1/admin/package-exclusions", json={"scope": "global", "pattern": "x"}
    ).status_code == 422  # missing header
    r = client.post(
        "/api/v1/admin/package-exclusions",
        headers={"X-Admin-Key": "wrong"},
        json={"scope": "global", "pattern": "x"},
    )
    assert r.status_code == 401


def test_list_scoped_to_a_host_includes_global(client):
    host_a, _ = create_host(client, hostname="a")
    host_b, _ = create_host(client, hostname="b")
    _create(client, pattern="docker-ce")  # global
    _create(client, scope="host", host_id=host_a, pattern="postgresql-14")
    _create(client, scope="host", host_id=host_b, pattern="nvidia*")

    r = client.get(f"/api/v1/package-exclusions?host_id={host_a}")
    assert r.status_code == 200
    patterns = {row["pattern"] for row in r.json()}
    assert patterns == {"docker-ce", "postgresql-14"}

    r = client.get("/api/v1/package-exclusions")
    assert {row["pattern"] for row in r.json()} == {"docker-ce", "postgresql-14", "nvidia*"}


def test_get_single_and_404(client):
    eid = _create(client).json()["id"]
    assert client.get(f"/api/v1/package-exclusions/{eid}").status_code == 200
    assert client.get(f"/api/v1/package-exclusions/{uuid.uuid4()}").status_code == 404


def test_delete(client):
    eid = _create(client).json()["id"]
    r = client.delete(f"/api/v1/admin/package-exclusions/{eid}", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert client.get(f"/api/v1/package-exclusions/{eid}").status_code == 404
    assert client.delete(
        f"/api/v1/admin/package-exclusions/{uuid.uuid4()}", headers=ADMIN_HEADERS
    ).status_code == 404


def test_cascade_on_host_delete(client, db_session):
    host_id, _ = create_host(client)
    eid = _create(client, scope="host", host_id=host_id, pattern="postgresql-14").json()["id"]

    r = client.delete(f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS)
    assert r.status_code == 204
    assert db_session.get(PackageExclusion, uuid.UUID(eid)) is None


def test_audit_trail(client, db_session):
    eid = _create(client).json()["id"]
    client.delete(f"/api/v1/admin/package-exclusions/{eid}", headers=ADMIN_HEADERS)

    actions = [
        a
        for (a,) in db_session.query(AuditLog.action)
        .filter(AuditLog.target_type == "package_exclusion")
        .order_by(AuditLog.id)
        .all()
    ]
    assert actions == ["package_exclusion.create", "package_exclusion.delete"]
    create_detail = (
        db_session.query(AuditLog.detail)
        .filter(AuditLog.action == "package_exclusion.create")
        .scalar()
    )
    assert create_detail["pattern"] == "linux-image*"
    assert create_detail["tag"] is None  # global rule


def test_audit_trail_records_the_tag(client, db_session):
    _create(client, scope="tag", tag="role=web", pattern="nginx*")
    create_detail = (
        db_session.query(AuditLog.detail)
        .filter(AuditLog.action == "package_exclusion.create")
        .scalar()
    )
    assert create_detail["scope"] == "tag"
    assert create_detail["tag"] == "role=web"
