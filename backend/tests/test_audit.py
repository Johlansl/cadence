"""The audit_log schema (migration 0007) and the record_audit() helper.

Wiring record_audit() into the admin handlers is covered in test_admin.py
once the calls land; here we check the table matches the ORM model and the
helper derives actor / client / request_id correctly without committing.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from starlette.requests import Request

from app.api.audit import record_audit
from app.models.models import AuditLog


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
