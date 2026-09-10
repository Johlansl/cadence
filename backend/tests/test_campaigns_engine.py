"""app.campaigns.engine.advance_campaigns -- the per-tick campaign engine.

Campaigns are created and activated through the A3 routes; job outcomes are
simulated by setting jobs.status / failure_category / completed_at directly,
since no real agent runs in the test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.campaigns import engine
from app.campaigns.engine import advance_campaigns
from app.models.models import Campaign, CampaignHost, Host, Job, WebhookDelivery
from tests.conftest import ADMIN_HEADERS, webhook_row

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _hosts(db_session, *names):
    made = [Host(hostname=n) for n in names]
    db_session.add_all(made)
    db_session.flush()
    return made


def _running(client, db_session, names, stages, **kw):
    hs = _hosts(db_session, *names)
    body = {
        "name": kw.get("name", "eng"),
        "host_ids": [str(h.id) for h in hs],
        "stages": stages,
        "max_concurrency": kw.get("max_concurrency", 1),
        "max_failures": kw.get("max_failures", 0),
    }
    if "observation_window_seconds" in kw:
        body["observation_window_seconds"] = kw["observation_window_seconds"]
    r = client.post("/api/v1/admin/campaigns", headers=ADMIN_HEADERS, json=body)
    assert r.status_code == 201, r.text
    cid = uuid.UUID(r.json()["id"])
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/activate", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    return cid, hs


def _ch(db_session, cid, host_id) -> CampaignHost:
    return db_session.execute(
        select(CampaignHost).where(
            CampaignHost.campaign_id == cid, CampaignHost.host_id == host_id
        )
    ).scalar_one()


def _finish(
    db_session,
    cid,
    host_id,
    *,
    status="succeeded",
    category=None,
    health="healthy",
    at=NOW,
):
    ch = _ch(db_session, cid, host_id)
    job = db_session.get(Job, ch.job_id)
    job.status = status
    job.completed_at = at
    if status == "failed":
        job.failure_category = category
    elif health is not None:
        job.result = {"health_status": health}
    db_session.flush()


def _status(db_session, cid) -> str:
    return db_session.get(Campaign, cid).status


# --- concurrency + stage progression ---------------------------------------


def test_fills_active_stage_up_to_max_concurrency(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b", "c"], [3], max_concurrency=2, max_failures=3
    )

    advance_campaigns(now=NOW, db=db_session)

    states = sorted(_ch(db_session, cid, h.id).state for h in hs)
    assert states == ["pending", "running", "running"]
    campaign_jobs = db_session.execute(
        select(Job).where(Job.campaign_id == cid)
    ).scalars().all()
    assert len(campaign_jobs) == 2


def test_a_free_slot_opens_when_a_job_finishes(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b", "c"], [3], max_concurrency=2, max_failures=3
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id)  # a -> done

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert sorted(_ch(db_session, cid, h.id).state for h in hs) == [
        "done",
        "running",
        "running",
    ]


def test_stage_gate_holds_until_the_observation_window_elapses(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=300
    )
    advance_campaigns(now=NOW, db=db_session)  # stage 0 (host a) -> running
    _finish(db_session, cid, hs[0].id, at=NOW)  # a done at NOW

    advance_campaigns(now=NOW + timedelta(seconds=200), db=db_session)  # still inside window
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "pending"  # stage 1 not started

    advance_campaigns(now=NOW + timedelta(seconds=301), db=db_session)  # window elapsed
    assert _ch(db_session, cid, hs[1].id).state == "running"


def test_zero_window_advances_immediately(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[1].id).state == "running"


def test_empty_middle_stage_is_skipped(client, db_session):
    # 3 hosts, stages [1, "10%", "rest"] -> sizes 1, floor(0.3)=0, 2
    cid, hs = _running(
        client, db_session, ["a", "b", "c"], [1, "10%", "rest"],
        observation_window_seconds=0, max_concurrency=5,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW)  # stage 0 done
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    # engine jumped over the empty stage 1 straight to stage 2
    later = {_ch(db_session, cid, h.id).state for h in hs[1:]}
    assert later == {"running"}


def test_campaign_completes_after_the_last_stage_window(client, db_session):
    cid, (a,) = _running(
        client, db_session, ["a"], ["rest"], observation_window_seconds=60
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, a.id, at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=30), db=db_session)
    assert _status(db_session, cid) == "running"  # window still holding

    advance_campaigns(now=NOW + timedelta(seconds=61), db=db_session)
    c = db_session.get(Campaign, cid)
    assert c.status == "completed" and c.completed_at is not None


# --- dispositions --------------------------------------------------------------


def test_skip_disposition_drops_the_host_and_keeps_going(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [1, "rest"],
        max_failures=1, observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, status="failed", category="apt_locked", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    ch_a = _ch(db_session, cid, hs[0].id)
    assert ch_a.state == "skipped" and ch_a.skip_reason == "apt_locked"
    assert _status(db_session, cid) == "running"
    assert _ch(db_session, cid, hs[1].id).state == "running"  # stage advanced


def test_halt_disposition_stops_the_campaign_and_finalizes(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)  # a, b jobs created
    # an agent has claimed b's job (it is genuinely in flight)
    ch_b = _ch(db_session, cid, hs[1].id)
    db_session.get(Job, ch_b.job_id).status = "running"
    db_session.flush()
    _finish(db_session, cid, hs[0].id, status="failed", category="network_or_repo", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    c = db_session.get(Campaign, cid)
    assert c.status == "stopped"
    assert "network_or_repo" in c.halt_reason and "a" in c.halt_reason
    # the failing host is skipped, its still-in-flight sibling is orphaned
    assert _ch(db_session, cid, hs[0].id).state == "skipped"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"
    assert db_session.get(Job, ch_b.job_id).status == "running"  # job left untouched


def test_unknown_failure_category_halts_fail_safe(client, db_session):
    cid, (a,) = _running(client, db_session, ["a"], ["rest"], max_failures=5)
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, a.id, status="failed", category=None, at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "stopped"


def test_unhealthy_success_stops_the_campaign(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, health="unhealthy", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason == "health_unhealthy on a"
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"


def test_unknown_or_missing_health_stops_the_campaign_fail_safe(client, db_session):
    for health in ("unknown", None):
        cid, (host,) = _running(
            client,
            db_session,
            [f"host-{health}"],
            ["rest"],
            max_failures=5,
            name=f"campaign-{health}",
        )
        advance_campaigns(now=NOW, db=db_session)
        _finish(db_session, cid, host.id, health=health, at=NOW)

        advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

        campaign = db_session.get(Campaign, cid)
        assert campaign.status == "stopped"
        assert campaign.halt_reason.startswith("health_unknown on ")


def test_degraded_health_does_not_stop_the_campaign(client, db_session):
    cid, (host,) = _running(
        client,
        db_session,
        ["a"],
        ["rest"],
        observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, host.id, health="degraded", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    assert _status(db_session, cid) == "completed"


def test_max_failures_boundary(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b", "c"], [3],
        max_concurrency=3, max_failures=1, observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, status="failed", category="timeout", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "running"  # 1 skip == max_failures, not over

    _finish(db_session, cid, hs[1].id, status="failed", category="disk_full", at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)
    assert _status(db_session, cid) == "stopped"  # 2 > 1
    assert db_session.get(Campaign, cid).halt_reason == "max_failures exceeded"


# --- interaction rules -------------------------------------------------------


def test_host_with_a_pre_existing_active_job_is_retried_not_failed(client, db_session):
    cid, (a,) = _running(client, db_session, ["a"], ["rest"])
    db_session.add(Job(host_id=a.id, job_type="apt_upgrade", status="pending"))
    db_session.flush()

    advance_campaigns(now=NOW, db=db_session)

    ch = _ch(db_session, cid, a.id)
    assert ch.state == "pending" and ch.job_id is None
    assert _status(db_session, cid) == "running"
    assert (
        db_session.execute(
            select(Job).where(Job.campaign_id == cid)
        ).first()
        is None
    )


def test_paused_campaign_is_not_touched(client, db_session):
    cid, (a,) = _running(client, db_session, ["a"], ["rest"])
    advance_campaigns(now=NOW, db=db_session)
    assert client.post(
        f"/api/v1/admin/campaigns/{cid}/pause", headers=ADMIN_HEADERS
    ).status_code == 200
    _finish(db_session, cid, a.id, at=NOW)

    advance_campaigns(now=NOW + timedelta(hours=1), db=db_session)

    assert _ch(db_session, cid, a.id).state == "running"  # not reconciled while paused
    assert _status(db_session, cid) == "paused"


def _deliveries(db_session, event_type):
    return (
        db_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == event_type)
        )
        .scalars()
        .all()
    )


def test_emits_stage_completed_and_completed_events(client, db_session):
    webhook_row(
        db_session,
        events=("campaign.stage_completed", "campaign.completed", "campaign.stopped"),
    )
    cid, hs = _running(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    _finish(db_session, cid, hs[1].id, at=NOW + timedelta(seconds=1))
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)

    stage_evts = _deliveries(db_session, "campaign.stage_completed")
    assert sorted(d.payload["data"]["stage_index"] for d in stage_evts) == [0, 1]
    completed = _deliveries(db_session, "campaign.completed")
    assert len(completed) == 1
    assert completed[0].payload["data"]["hosts_done"] == 2
    assert _deliveries(db_session, "campaign.stopped") == []


def test_emits_stopped_event_with_halt_category_and_host(client, db_session):
    webhook_row(db_session, events=("campaign.stopped",))
    cid, (a,) = _running(client, db_session, ["a"], ["rest"], max_failures=5)
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, a.id, status="failed", category="network_or_repo", at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    (evt,) = _deliveries(db_session, "campaign.stopped")
    data = evt.payload["data"]
    assert data["halt_category"] == "network_or_repo"
    assert data["halt_host"] == "a"
    assert "network_or_repo" in data["reason"]


def test_emits_stopped_event_for_unhealthy_success(client, db_session):
    webhook_row(db_session, events=("campaign.stopped",))
    cid, (host,) = _running(client, db_session, ["a"], ["rest"], max_failures=5)
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, host.id, health="unhealthy", at=NOW)

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)

    (event,) = _deliveries(db_session, "campaign.stopped")
    assert event.payload["data"]["halt_category"] == "health_unhealthy"
    assert event.payload["data"]["halt_host"] == "a"


def test_one_campaigns_error_does_not_block_the_others(client, db_session, monkeypatch):
    good, _ = _running(client, db_session, ["g1"], ["rest"], name="good")
    bad, _ = _running(client, db_session, ["b1"], ["rest"], name="bad")

    real = engine._advance_one

    def flaky(db, c, now):
        if c.id == bad:
            raise RuntimeError("boom")
        return real(db, c, now)

    monkeypatch.setattr(engine, "_advance_one", flaky)
    advanced = advance_campaigns(now=NOW, db=db_session)

    assert advanced == 1
    assert _status(db_session, good) == "running"
    assert _ch(db_session, good, db_session.execute(
        select(Host.id).where(Host.hostname == "g1")
    ).scalar_one()).state == "running"
    # the bad campaign rolled back untouched, still running, no leaked job
    assert _status(db_session, bad) == "running"
