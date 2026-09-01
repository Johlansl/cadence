"""The audit_log schema (migration 0007) and the record_audit() helper.

Wiring record_audit() into the admin handlers is covered in test_admin.py
once the calls land; here we check the table matches the ORM model and the
helper derives actor / client / request_id correctly without committing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from starlette.requests import Request

from app.api.audit import record_audit
from app.models.models import AuditLog, Host
from tests.conftest import ADMIN_HEADERS, create_host

WEEKLY = {"kind": "weekly", "weekday": 6, "hour": 3, "minute": 0, "timezone": "UTC"}


def _audit(db, **where):
    stmt = select(AuditLog).order_by(AuditLog.id)
    for col, val in where.items():
        stmt = stmt.where(getattr(AuditLog, col) == val)
    return list(db.execute(stmt).scalars().all())


def _request(headers: dict[str, str], *, client_host: str = "10.9.9.9") -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/admin/hosts",
            "headers": raw,
            "client": (client_host, 40000),
        }
    )
    req.state.request_id = "req-abc123"
    return req


def test_audit_row_round_trips_through_the_orm(db_session):
    db_session.add(
        AuditLog(action="host.create", target_type="host", target_id="x", detail={"a": 1})
    )
    db_session.flush()
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.id is not None
    assert row.at is not None  # server_default now()
    assert row.actor == "admin"  # column default
    assert row.detail == {"a": 1}


def test_record_audit_stages_without_committing(db_session):
    req = _request({"x-forwarded-for": "203.0.113.7, 10.0.0.1"})
    host_id = uuid.uuid4()

    record_audit(
        db_session, req, "host.delete", target_type="host", target_id=host_id
    )

    # Nothing persisted yet: the helper only db.add()s.
    assert any(isinstance(o, AuditLog) for o in db_session.new)
    assert db_session.execute(select(func.count()).select_from(AuditLog)).scalar() == 0

    db_session.flush()
    row = db_session.execute(select(AuditLog)).scalar_one()
    assert row.action == "host.delete"
    assert row.target_type == "host"
    assert row.target_id == str(host_id)  # stringified
    assert row.actor == "admin"  # no X-Actor header
    assert row.client == "203.0.113.7"  # first X-Forwarded-For hop, not 10.9.9.9
    assert row.request_id == "req-abc123"


def test_record_audit_uses_x_actor_header_when_present(db_session):
    record_audit(db_session, _request({"x-actor": "alice"}), "schedule.update")
    db_session.flush()
    assert db_session.execute(select(AuditLog.actor)).scalar_one() == "alice"


def test_record_audit_blank_x_actor_falls_back_to_admin(db_session):
    record_audit(db_session, _request({"x-actor": "   "}), "job.create")
    db_session.flush()
    assert db_session.execute(select(AuditLog.actor)).scalar_one() == "admin"


# --- the eight admin handlers, end to end -----------------------------------


def test_host_lifecycle_is_audited(client, db_session):
    r = client.post(
        "/api/v1/admin/hosts",
        headers={**ADMIN_HEADERS, "X-Actor": "alice"},
        json={"hostname": "vm-audit"},
    )
    host_id = r.json()["id"]

    client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"is_active": False},
    )
    client.delete(f"/api/v1/admin/hosts/{host_id}", headers=ADMIN_HEADERS)

    rows = _audit(db_session, target_type="host")
    assert [row.action for row in rows] == ["host.create", "host.update", "host.delete"]
    assert all(row.target_id == host_id for row in rows)
    assert rows[0].actor == "alice"  # X-Actor flowed through
    assert rows[1].actor == "admin"  # header absent -> default
    assert rows[1].detail == {"fields": ["is_active"]}
    # the delete row survives the cascade that removed the host
    assert rows[2].detail == {"hostname": "vm-audit"}
    assert db_session.get(Host, host_id) is None


def test_failed_mutation_writes_no_audit_row(client, db_session):
    r = client.patch(
        f"/api/v1/admin/hosts/{uuid.uuid4()}",
        headers=ADMIN_HEADERS,
        json={"is_active": False},
    )
    assert r.status_code == 404
    assert _audit(db_session) == []


def test_rejected_admin_key_writes_no_audit_row(client, db_session):
    r = client.post(
        "/api/v1/admin/hosts", headers={"X-Admin-Key": "wrong"}, json={"hostname": "x"}
    )
    assert r.status_code == 401
    assert _audit(db_session) == []


def test_job_create_and_clear_are_audited(client, db_session):
    host_id, _ = create_host(client)

    r = client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    job_id = r.json()["id"]
    client.delete(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS)

    created = _audit(db_session, action="job.create")[0]
    assert created.target_type == "job" and created.target_id == job_id
    assert created.detail["host_id"] == host_id and created.detail["job_type"] == "apt_upgrade"

    cleared = _audit(db_session, action="job.clear")[0]
    assert cleared.target_type == "host" and cleared.target_id == host_id
    assert cleared.detail == {"deleted": 1}


def test_schedule_lifecycle_is_audited(client, db_session):
    host_id, _ = create_host(client)

    r = client.post(
        f"/api/v1/admin/hosts/{host_id}/schedules", headers=ADMIN_HEADERS, json=WEEKLY
    )
    sid = r.json()["id"]
    client.patch(
        f"/api/v1/admin/schedules/{sid}", headers=ADMIN_HEADERS, json={"enabled": False}
    )
    client.delete(f"/api/v1/admin/schedules/{sid}", headers=ADMIN_HEADERS)

    rows = _audit(db_session, target_type="schedule")
    assert [row.action for row in rows] == [
        "schedule.create",
        "schedule.update",
        "schedule.delete",
    ]
    assert all(row.target_id == sid for row in rows)
    assert rows[1].detail == {"fields": ["enabled"]}
    assert rows[2].detail == {"host_id": host_id}


def test_audit_row_carries_request_id_from_the_middleware(client, db_session):
    client.post(
        "/api/v1/admin/hosts",
        headers={**ADMIN_HEADERS, "X-Request-ID": "trace-42"},
        json={"hostname": "vm-trace"},
    )
    row = _audit(db_session, action="host.create")[0]
    assert row.request_id == "trace-42"


# --- GET /api/v1/admin/audit ----------------------------------------------


def test_audit_endpoint_requires_admin_key(client):
    assert client.get("/api/v1/admin/audit").status_code == 422  # header missing
    assert (
        client.get("/api/v1/admin/audit", headers={"X-Admin-Key": "no"}).status_code
        == 401
    )


def test_audit_endpoint_lists_newest_first_and_filters(client, db_session):
    host_id, _ = create_host(client)
    client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})
    client.delete(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS)

    rows = client.get("/api/v1/admin/audit", headers=ADMIN_HEADERS).json()
    actions = [r["action"] for r in rows]
    assert actions == ["job.clear", "job.create", "host.create"]  # newest first
    assert rows[-1]["actor"] == "admin"

    only_create = client.get(
        "/api/v1/admin/audit?action=job.create", headers=ADMIN_HEADERS
    ).json()
    assert [r["action"] for r in only_create] == ["job.create"]

    by_target = client.get(
        f"/api/v1/admin/audit?target_type=host&target_id={host_id}",
        headers=ADMIN_HEADERS,
    ).json()
    assert {r["action"] for r in by_target} == {"host.create", "job.clear"}


def test_audit_endpoint_keyset_pages_through_a_shared_timestamp(client, db_session):
    from app.models.models import AuditLog as _AL

    t = datetime(2026, 3, 1, tzinfo=timezone.utc)
    stamps = [t, t + timedelta(seconds=1), t + timedelta(seconds=1), t + timedelta(seconds=1), t + timedelta(seconds=2)]
    for i, at in enumerate(stamps):
        db_session.add(_AL(at=at, action=f"test.{i}", actor="admin"))
    db_session.commit()

    seen: list[int] = []
    url = "/api/v1/admin/audit?limit=2"
    for _ in range(10):
        page = client.get(url, headers=ADMIN_HEADERS).json()
        seen.extend(r["id"] for r in page)
        if len(page) < 2:
            break
        last = page[-1]
        url = f"/api/v1/admin/audit?limit=2&before={last['at']}&before_id={last['id']}"

    full = [r["id"] for r in client.get("/api/v1/admin/audit?limit=500", headers=ADMIN_HEADERS).json()]
    assert seen == full
    assert len(set(seen)) == len(full) >= 5
