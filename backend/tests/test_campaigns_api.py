"""Campaign create / read / lifecycle routes (roadmap item 5, sub-delivery A3).

The engine does not exist yet, so a "mid-run" campaign is simulated by hand
(a Job row + campaign_hosts.job_id / state) where a test needs one.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select, text, update

from app.models.models import AuditLog, Campaign, CampaignHost, Host, Job
from tests.conftest import ADMIN_HEADERS


def _hosts(db_session, *names, tags=None):
    made = []
    for n in names:
        h = Host(hostname=n, tags=tags or {})
        db_session.add(h)
        made.append(h)
    db_session.flush()
    return made


def _create(client, **body):
    return client.post("/api/v1/admin/campaigns", headers=ADMIN_HEADERS, json=body)


# --- create + targeting ----------------------------------------------------


def test_create_with_explicit_hosts_slices_stages(client, db_session):
    a, b, c = _hosts(db_session, "vm-c", "vm-a", "vm-b")  # unsorted on purpose
    r = _create(
        client,
        name="patch tuesday",
        host_ids=[str(a.id), str(b.id), str(c.id)],
        stages=[1, "rest"],
        max_concurrency=2,
        max_failures=1,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft"
    assert body["observation_window_seconds"] == 600  # settings default
    assert body["hosts_total"] == 3
    assert body["current_stage_index"] == 0

    rows = sorted(
        db_session.execute(
            select(CampaignHost.stage_index, Host.hostname)
            .join(Host, Host.id == CampaignHost.host_id)
            .where(CampaignHost.campaign_id == uuid.UUID(body["id"]))
        ).all(),
        key=lambda t: t[1],
    )
    # hostname order vm-a, vm-b, vm-c -> stage 0 gets the first, rest to stage 1
    assert rows == [(0, "vm-a"), (1, "vm-b"), (1, "vm-c")]
    assert body["stages_detail"][0] == {
        "index": 0,
        "size_spec": 1,
        "hosts_total": 1,
        "pending": 1,
        "running": 0,
        "done": 0,
        "skipped": 0,
        "orphaned": 0,
    }


def test_create_with_tag_filter(client, db_session):
    _hosts(db_session, "web-1", "web-2", tags={"role": "web"})
    _hosts(db_session, "db-1", tags={"role": "db"})
    r = _create(
        client, name="web only", tag="role=web", stages=["rest"], max_concurrency=1, max_failures=0
    )
    assert r.status_code == 201, r.text
    assert r.json()["hosts_total"] == 2


def test_create_with_percent_stage(client, db_session):
    hs = _hosts(db_session, *[f"h{i:02d}" for i in range(10)])
    r = _create(
        client,
        name="ten",
        host_ids=[str(h.id) for h in hs],
        stages=[2, "50%", "rest"],
        max_concurrency=3,
        max_failures=2,
    )
    assert r.status_code == 201, r.text
    detail = r.json()
    sizes = {s["index"]: s["hosts_total"] for s in detail["stages_detail"]}
    assert sizes == {0: 2, 1: 5, 2: 3}  # 2, floor(50% of 10), the rest


def test_create_observation_window_override(client, db_session):
    (h,) = _hosts(db_session, "solo")
    r = _create(
        client,
        name="fast",
        host_ids=[str(h.id)],
        stages=["rest"],
        max_concurrency=1,
        max_failures=0,
        observation_window_seconds=0,
    )
    assert r.status_code == 201
    assert r.json()["observation_window_seconds"] == 0


def test_create_rejects_bad_admin_key(client, db_session):
    (h,) = _hosts(db_session, "solo")
    r = client.post(
        "/api/v1/admin/campaigns",
        headers={"X-Admin-Key": "wrong"},
        json={
            "name": "x",
            "host_ids": [str(h.id)],
            "stages": ["rest"],
            "max_concurrency": 1,
            "max_failures": 0,
        },
    )
    assert r.status_code == 401


def test_create_rejects_bad_targeting_and_stages(client, db_session):
    (h,) = _hosts(db_session, "solo")
    good = dict(stages=["rest"], max_concurrency=1, max_failures=0)

    # neither target
    assert _create(client, name="x", **good).status_code == 422
    # both targets
    assert _create(
        client, name="x", host_ids=[str(h.id)], tag="role=web", **good
    ).status_code == 422
    # unknown host id
    assert _create(
        client, name="x", host_ids=[str(uuid.uuid4())], **good
    ).status_code == 422
    # tag matches nothing
    assert _create(
        client, name="x", tag="role=nope", **good
    ).status_code == 422
    # "rest" not last
    assert _create(
        client, name="x", host_ids=[str(h.id)], stages=["rest", 1],
        max_concurrency=1, max_failures=0,
    ).status_code == 422
    # bad percent
    assert _create(
        client, name="x", host_ids=[str(h.id)], stages=["200%"],
        max_concurrency=1, max_failures=0,
    ).status_code == 422
    # stages leave a host uncovered, no "rest"
    two = _hosts(db_session, "u1", "u2")
    assert _create(
        client, name="x", host_ids=[str(two[0].id), str(two[1].id)], stages=[1],
        max_concurrency=1, max_failures=0,
    ).status_code == 422


def test_create_is_audited(client, db_session):
    (h,) = _hosts(db_session, "solo")
    _create(
        client, name="audited", host_ids=[str(h.id)], stages=["rest"],
        max_concurrency=1, max_failures=0,
    )
    row = db_session.execute(
        select(AuditLog).where(AuditLog.action == "campaign.create")
    ).scalar_one()
    assert row.target_type == "campaign"
    assert row.detail["hosts"] == 1


# --- lifecycle -----------------------------------------------------------------


def _draft(client, db_session, name="lc", n=2, **over):
    hs = _hosts(db_session, *[f"{name}-{i}" for i in range(n)])
    body = dict(
        name=name,
        host_ids=[str(h.id) for h in hs],
        stages=[1, "rest"],
        max_concurrency=1,
        max_failures=1,
    )
    body.update(over)
    r = _create(client, **body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _act(client, cid, verb):
    return client.post(f"/api/v1/admin/campaigns/{cid}/{verb}", headers=ADMIN_HEADERS)


def test_activate_pause_resume_cancel_happy_path(client, db_session):
    cid = _draft(client, db_session)

    r = _act(client, cid, "activate")
    assert r.status_code == 200 and r.json()["status"] == "running"
    assert r.json()["started_at"] is not None

    assert _act(client, cid, "pause").json()["status"] == "paused"
    assert _act(client, cid, "resume").json()["status"] == "running"

    r = _act(client, cid, "cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["completed_at"] is not None


def test_transition_conflicts(client, db_session):
    cid = _draft(client, db_session)
    assert _act(client, cid, "pause").status_code == 409  # not running
    assert _act(client, cid, "resume").status_code == 409  # not paused
    _act(client, cid, "activate")
    assert _act(client, cid, "activate").status_code == 409  # not draft
    _act(client, cid, "cancel")
    assert _act(client, cid, "cancel").status_code == 409  # already terminal
    assert _act(client, cid, "activate").status_code == 409


def test_actions_reject_bad_admin_key_and_unknown_id(client, db_session):
    cid = _draft(client, db_session)
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/activate",
            headers={"X-Admin-Key": "wrong"},
        ).status_code
        == 401
    )
    assert _act(client, str(uuid.uuid4()), "activate").status_code == 404


def test_each_action_is_audited(client, db_session):
    cid = _draft(client, db_session)
    for verb in ("activate", "pause", "resume", "cancel"):
        _act(client, cid, verb)
    actions = set(
        db_session.execute(
            select(AuditLog.action).where(AuditLog.action.like("campaign.%"))
        ).scalars()
    )
    assert actions == {
        "campaign.create",
        "campaign.activate",
        "campaign.pause",
        "campaign.resume",
        "campaign.cancel",
    }


def test_cancel_finalizes_in_flight_and_unstarted_hosts(client, db_session):
    cid = _draft(client, db_session, n=3)
    _act(client, cid, "activate")

    ch_rows = db_session.execute(
        select(CampaignHost).where(CampaignHost.campaign_id == uuid.UUID(cid))
    ).scalars().all()
    ch_rows.sort(key=lambda c: c.stage_index)

    # host 0: a job still running
    running = Job(host_id=ch_rows[0].host_id, job_type="apt_upgrade", status="running")
    db_session.add(running)
    db_session.flush()
    ch_rows[0].job_id = running.id
    ch_rows[0].state = "running"
    # host 1: a job that already succeeded
    done = Job(host_id=ch_rows[1].host_id, job_type="apt_upgrade", status="succeeded")
    db_session.add(done)
    db_session.flush()
    ch_rows[1].job_id = done.id
    ch_rows[1].state = "running"
    # host 2: never started (still pending, no job)
    db_session.flush()

    r = _act(client, cid, "cancel")
    assert r.status_code == 200

    by_host = {
        c.host_id: c
        for c in db_session.execute(
            select(CampaignHost).where(CampaignHost.campaign_id == uuid.UUID(cid))
        ).scalars()
    }
    assert by_host[ch_rows[0].host_id].state == "orphaned"  # job still in flight
    assert by_host[ch_rows[1].host_id].state == "done"  # reconciled to real outcome
    assert by_host[ch_rows[2].host_id].state == "orphaned"  # never got a job
    # the running job is untouched
    assert db_session.get(Job, running.id).status == "running"


def test_cancel_never_marks_unproven_reboot_done(client, db_session):
    # Submit -> reconcile window: both jobs succeeded with a reboot the
    # engine never saw (no await marker yet). Cancel must orphan them,
    # never label them done without proof.
    cid = _draft(client, db_session, n=2)
    _act(client, cid, "activate")

    ch_rows = (
        db_session.execute(
            select(CampaignHost).where(CampaignHost.campaign_id == uuid.UUID(cid))
        )
        .scalars()
        .all()
    )
    ch_rows.sort(key=lambda c: c.stage_index)

    new_agent = Job(
        host_id=ch_rows[0].host_id,
        job_type="apt_upgrade",
        status="succeeded",
        params={"reboot": "auto"},
        result={"reboot_required": True, "will_reboot": True, "boot_id": "b0"},
    )
    old_agent = Job(
        host_id=ch_rows[1].host_id,
        job_type="apt_upgrade",
        status="succeeded",
        params={"reboot": "auto"},
        result={"reboot_required": True},  # no proof fields at all
    )
    db_session.add_all([new_agent, old_agent])
    db_session.flush()
    for ch, job in zip([ch_rows[0], ch_rows[1]], [new_agent, old_agent], strict=True):
        ch.job_id = job.id
        ch.state = "running"
    db_session.flush()

    assert _act(client, cid, "cancel").status_code == 200

    states = {
        c.host_id: c.state
        for c in db_session.execute(
            select(CampaignHost).where(CampaignHost.campaign_id == uuid.UUID(cid))
        ).scalars()
    }
    assert states[ch_rows[0].host_id] == "orphaned"
    assert states[ch_rows[1].host_id] == "orphaned"


# --- reads -------------------------------------------------------------------


def test_list_is_newest_first_and_detail_404(client, db_session):
    older = _draft(client, db_session, name="older")
    _draft(client, db_session, name="newer")
    # In tests every commit shares one transaction, so now() (and thus
    # created_at) is identical for both rows; age one explicitly to pin order.
    db_session.execute(
        update(Campaign)
        .where(Campaign.id == uuid.UUID(older))
        .values(created_at=text("now() - interval '1 hour'"))
    )
    db_session.flush()
    names = [c["name"] for c in client.get("/api/v1/campaigns").json()]
    assert names.index("newer") < names.index("older")
    assert client.get(f"/api/v1/campaigns/{uuid.uuid4()}").status_code == 404


# --- C1: agent_upgrade contract (persisted, not executable) ---------------------


def _agent_body(h, **over):
    body = dict(
        name="fleet",
        job_type="agent_upgrade",
        job_params={"target_version": "0.15.0"},
        host_ids=[str(h.id)],
        stages=["rest"],
        max_concurrency=1,
        max_failures=0,
    )
    body.update(over)
    return body


def test_create_without_new_fields_stays_apt_with_empty_params(client, db_session):
    (h,) = _hosts(db_session, "legacy")
    r = _create(
        client, name="legacy", host_ids=[str(h.id)], stages=["rest"],
        max_concurrency=1, max_failures=0,
    )
    assert r.status_code == 201, r.text
    assert r.json()["job_type"] == "apt_upgrade"
    assert r.json()["job_params"] == {}
    row = db_session.get(Campaign, uuid.UUID(r.json()["id"]))
    assert row.job_type == "apt_upgrade" and row.job_params == {}


def test_create_apt_with_nonempty_params_is_rejected(client, db_session):
    (h,) = _hosts(db_session, "noisy")
    r = _create(
        client, name="noisy", host_ids=[str(h.id)], stages=["rest"],
        max_concurrency=1, max_failures=0,
        job_params={"target_version": "0.15.0"},
    )
    assert r.status_code == 422, r.text


def test_create_agent_persists_exact_type_and_target(client, db_session):
    (h,) = _hosts(db_session, "fleet-0")
    r = _create(client, **_agent_body(h))
    assert r.status_code == 201, r.text
    assert r.json()["job_type"] == "agent_upgrade"
    assert r.json()["job_params"] == {"target_version": "0.15.0"}
    row = db_session.get(Campaign, uuid.UUID(r.json()["id"]))
    assert row.job_type == "agent_upgrade"
    assert row.job_params == {"target_version": "0.15.0"}
    detail = client.get(f"/api/v1/campaigns/{row.id}").json()
    assert detail["job_params"] == {"target_version": "0.15.0"}


def test_create_agent_is_audited(client, db_session):
    (h,) = _hosts(db_session, "audited-agent")
    r = _create(client, **_agent_body(h, name="audited-agent"))
    assert r.status_code == 201, r.text
    row = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "campaign.create",
            AuditLog.target_id == r.json()["id"],
        )
    ).scalar_one()
    assert row.target_type == "campaign"
    # The audit detail carries name/hosts/stages (historic shape); the exact
    # target stays readable from the persisted campaign row itself.


_TRAP_KEYS = [
    "url", "sha256", "checksum", "pubkey", "path", "command", "cmd",
    "args", "base_url", "allow_downgrade",
]


def test_create_agent_rejects_every_trap_key(client, db_session):
    (h,) = _hosts(db_session, "trap")
    for key in _TRAP_KEYS:
        params = {"target_version": "0.15.0", key: "x"}
        r = _create(client, **_agent_body(h, job_params=params))
        assert r.status_code == 422, (key, r.text)


def test_create_agent_rejects_bad_targets(client, db_session):
    (h,) = _hosts(db_session, "badtgt")
    bad = [
        "01.2.3", "1.02.3", "1.2.03",  # non-canonical leading zeros
        "v0.15.0", "0.15.0-rc1", "0.15.0+build", " 0.15.0", "0.15.0 ",
        "", "0.15", "0.15.0.1", "x" * 65,
    ]
    for target in bad:
        r = _create(client, **_agent_body(h, job_params={"target_version": target}))
        assert r.status_code == 422, (target, r.text)
    for params in ({}, {"target_version": 15}, {"target_version": None}):
        r = _create(client, **_agent_body(h, job_params=params))
        assert r.status_code == 422, (params, r.text)


def test_create_rejects_non_campaign_job_types(client, db_session):
    (h,) = _hosts(db_session, "reboot-camp")
    for t in ("reboot", "health_check", "apt_dry_run", "nope"):
        r = _create(client, **_agent_body(h, job_type=t))
        assert r.status_code == 422, (t, r.text)


def test_activate_agent_campaign_runs_and_creates_no_job_yet(client, db_session):
    (h,) = _hosts(db_session, "unblocked")
    cid = _create(client, **_agent_body(h)).json()["id"]
    r = _act(client, cid, "activate")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    assert r.json()["job_params"] == {"target_version": "0.15.0"}
    row = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "campaign.activate",
            AuditLog.target_id == cid,
        )
    ).scalar_one()
    assert row.target_type == "campaign"  # target recoverable from the row itself


def test_lifecycle_never_rewrites_job_contract(client, db_session):
    (h,) = _hosts(db_session, "frozen")
    cid = _create(client, **_agent_body(h)).json()["id"]
    for verb in ("pause", "resume", "cancel", "pause", "cancel"):
        # pause/resume fail on a draft; cancel succeeds: only the status may
        # move, never the snapshotted contract.
        _act(client, cid, verb)
    row = db_session.get(Campaign, uuid.UUID(cid))
    assert row.status == "cancelled"
    assert row.job_type == "agent_upgrade"
    assert row.job_params == {"target_version": "0.15.0"}
