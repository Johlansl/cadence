"""app.campaigns.engine.advance_campaigns -- the per-tick campaign engine.

Campaigns are created and activated through the A3 routes; job outcomes are
simulated by setting jobs.status / failure_category / completed_at directly,
since no real agent runs in the test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.campaigns import engine
from app.campaigns.engine import advance_campaigns
from app.core.config import settings
from app.job_creation import _consumes_reboot_budget, _reboot_budget_in_use
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
    boot_id=None,
    will_reboot=None,
    reboot_required=None,
    proven=False,
):
    ch = _ch(db_session, cid, host_id)
    job = db_session.get(Job, ch.job_id)
    job.status = status
    job.completed_at = at
    if status == "failed":
        job.failure_category = category
    elif proven:
        job.result = {"proven": True}
    elif health is not None:
        result = {"health_status": health}
        if boot_id is not None:
            result["boot_id"] = boot_id
        if will_reboot is not None:
            result["will_reboot"] = will_reboot
        if reboot_required is not None:
            result["reboot_required"] = reboot_required
        job.result = result
    db_session.flush()


def _set_policy(client, host_id, value):
    r = client.patch(
        f"/api/v1/admin/hosts/{host_id}",
        headers=ADMIN_HEADERS,
        json={"reboot_policy": value},
    )
    assert r.status_code == 200, r.text


def _observe(db_session, host_id, *, boot=None, health=None, checked_at=None):
    """Simulate a host contact: a new boot observation and/or a fresh health
    projection, the way the report/result routes would record them."""
    host = db_session.get(Host, host_id)
    if boot is not None:
        host.current_boot_id = boot
    if health is not None:
        host.health_status = health
    if checked_at is not None:
        host.health_checked_at = checked_at
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


# --- 6A reboot await ---------------------------------------------------------


def _auto_running(client, db_session, names, stages, **kw):
    """A running campaign whose hosts all reboot on auto (pinned at fill)."""
    cid, hs = _running(client, db_session, names, stages, **kw)
    for h in hs:
        _set_policy(client, h.id, "auto")
    return cid, hs


def test_reboot_await_holds_row_running_until_proven(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)  # fill
    _finish(
        db_session, cid, hs[0].id, at=NOW,
        boot_id="boot-1", will_reboot=True, reboot_required=True,
    )

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    row = _ch(db_session, cid, hs[0].id)
    assert row.state == "running"
    assert row.awaited_boot == "boot-1"
    assert _status(db_session, cid) == "running"

    _observe(
        db_session, hs[0].id, boot="boot-2", health="healthy",
        checked_at=NOW + timedelta(seconds=2),
    )
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _status(db_session, cid) == "completed"


def test_await_needs_both_boot_change_and_fresh_health(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=2
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    _finish(db_session, cid, hs[1].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)  # both await

    _observe(db_session, hs[0].id, boot="boot-2")  # new boot, stale health
    _observe(  # fresh health, same boot
        db_session, hs[1].id, boot="boot-1", health="healthy",
        checked_at=NOW + timedelta(seconds=2),
    )
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "running"
    assert _ch(db_session, cid, hs[1].id).state == "running"


def test_fresh_unhealthy_after_reboot_stops_campaign(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)  # a awaits

    _observe(
        db_session, hs[0].id, boot="boot-2", health="unhealthy",
        checked_at=NOW + timedelta(seconds=2),
    )
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason == "health_unhealthy on a"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"


def test_degraded_fresh_health_completes_await(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    _observe(
        db_session, hs[0].id, boot="boot-2", health="degraded",
        checked_at=NOW + timedelta(seconds=2),
    )
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"


def test_explicit_no_reboot_completes_immediately(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(
        db_session, cid, hs[0].id, at=NOW,
        boot_id="boot-1", will_reboot=False, reboot_required=True,
    )
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    row = _ch(db_session, cid, hs[0].id)
    assert row.state == "done"
    assert row.awaited_boot is None


def test_expected_reboot_without_proof_halts_immediately(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)
    # Old agent: reboot pending on auto, but no boot fields at all.
    _finish(db_session, cid, hs[0].id, at=NOW, reboot_required=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason == "boot_proof_missing on a"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"


def test_unexpected_reboot_without_proof_completes(client, db_session):
    cid, hs = _running(client, db_session, ["a"], ["rest"], observation_window_seconds=0)
    advance_campaigns(now=NOW, db=db_session)
    # Never policy: no reboot follows, so no proof is needed.
    _finish(db_session, cid, hs[0].id, at=NOW, reboot_required=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"


def test_reboot_decision_without_boot_id_halts(client, db_session):
    cid, hs = _auto_running(client, db_session, ["a"], ["rest"])
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "stopped"
    assert db_session.get(Campaign, cid).halt_reason == "boot_proof_missing on a"


def test_cancel_orphans_unproven_await(client, db_session):
    cid, hs = _auto_running(client, db_session, ["a"], ["rest"])
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[0].id).awaited_boot == "boot-1"

    r = client.post(f"/api/v1/admin/campaigns/{cid}/cancel", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert _ch(db_session, cid, hs[0].id).state == "orphaned"
    assert _status(db_session, cid) == "cancelled"


def test_deleted_job_halts_instead_of_hanging(client, db_session):
    cid, hs = _running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)  # both running, jobs pending

    r = client.delete(f"/api/v1/admin/hosts/{hs[0].id}/jobs", headers=ADMIN_HEADERS)
    assert r.status_code == 204

    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason == "job_missing on a"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"


def test_await_survives_job_deletion_and_completes_on_proof(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[0].id).awaited_boot == "boot-1"

    client.delete(f"/api/v1/admin/hosts/{hs[0].id}/jobs", headers=ADMIN_HEADERS)
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)
    # The reference survives on the campaign row: still waiting, no halt, no hang.
    assert _ch(db_session, cid, hs[0].id).state == "running"

    _observe(
        db_session, hs[0].id, boot="boot-2", health="healthy",
        checked_at=NOW + timedelta(seconds=3),
    )
    advance_campaigns(now=NOW + timedelta(seconds=4), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"


def test_await_past_timeout_halts_fail_safe(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a", "b"], [2], max_concurrency=2, max_failures=5
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    entered = NOW + timedelta(seconds=1)
    advance_campaigns(now=entered, db=db_session)  # a awaits
    assert _ch(db_session, cid, hs[0].id).awaited_boot == "boot-1"

    span = timedelta(seconds=settings.campaign_return_timeout_seconds)
    advance_campaigns(now=entered + span, db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "running"  # boundary waits
    advance_campaigns(now=entered + span + timedelta(seconds=1), db=db_session)
    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason == "return_timeout on a"
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"


def test_await_holds_concurrency_slot(client, db_session):
    cid, hs = _auto_running(
        client, db_session, ["a", "b"], [2], max_concurrency=1, max_failures=1
    )
    advance_campaigns(now=NOW, db=db_session)  # a fills the only slot
    _finish(db_session, cid, hs[0].id, at=NOW, boot_id="boot-1", will_reboot=True)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)  # a awaits

    # No slot opens while a is unproven: b stays pending, not running.
    assert _ch(db_session, cid, hs[0].id).state == "running"
    assert _ch(db_session, cid, hs[1].id).state == "pending"

    _observe(
        db_session, hs[0].id, boot="boot-2", health="healthy",
        checked_at=NOW + timedelta(seconds=2),
    )
    advance_campaigns(now=NOW + timedelta(seconds=3), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "running"


# --- C2: agent_upgrade execution -----------------------------------------------


def _running_agent(client, db_session, names, stages, **kw):
    hs = _hosts(db_session, *names)
    body = {
        "name": kw.get("name", "agent-eng"),
        "job_type": "agent_upgrade",
        "job_params": {"target_version": kw.get("target", "0.15.0")},
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


def _job(db_session, cid, host_id) -> Job:
    return db_session.get(Job, _ch(db_session, cid, host_id).job_id)


def test_agent_fill_creates_typed_jobs_with_the_frozen_target(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [2], max_concurrency=2
    )
    advance_campaigns(now=NOW, db=db_session)
    for h in hs:
        job = _job(db_session, cid, h.id)
        assert job.job_type == "agent_upgrade"
        assert job.params == {"target_version": "0.15.0"}
        assert job.campaign_id == cid


def test_agent_proven_success_is_done_with_no_health_or_reboot(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(
        engine, "reboot_expected",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not be consulted")),
    )
    cid, (a,) = _running_agent(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    _set_policy(client, a.id, "auto")  # even an auto host takes no reboot path
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, a.id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    ch = _ch(db_session, cid, a.id)
    assert ch.state == "done" and ch.awaited_boot is None
    assert _status(db_session, cid) == "completed"


def test_agent_success_ignores_health_fields_that_would_halt_apt(
    client, db_session
):
    """Order guard: the agent branch sits before the apt health gate. A
    result that would halt an apt campaign must still complete here."""
    cid, (a,) = _running_agent(
        client, db_session, ["a"], ["rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    job = _job(db_session, cid, a.id)
    job.status = "succeeded"
    job.completed_at = NOW
    job.result = {"proven": True, "health_status": "unhealthy"}
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, a.id).state == "done"
    assert _status(db_session, cid) == "completed"


@pytest.mark.parametrize("category", ["agent_refused", "upgrade_install_failed"])
def test_agent_canary_local_skip_stops_with_zero_failures(
    client, db_session, category
):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"], max_failures=0
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, status="failed", category=category, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "stopped"
    assert _ch(db_session, cid, hs[0].id).state == "skipped"
    # The stop finalizes the unstarted sibling as orphaned: stage 2 never runs.
    assert _ch(db_session, cid, hs[1].id).state == "orphaned"
    assert _ch(db_session, cid, hs[1].id).job_id is None


def test_agent_fleet_tolerates_one_local_skip(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"],
        max_failures=1, observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(
        db_session, cid, hs[0].id, status="failed",
        category="upgrade_install_failed", at=NOW,
    )
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "running"
    assert _ch(db_session, cid, hs[0].id).state == "skipped"
    assert _ch(db_session, cid, hs[1].id).state == "running"  # stage advanced


@pytest.mark.parametrize(
    "category",
    ["upgrade_verification_failed", "upgrade_proof_timeout", "upgrade_download_failed"],
)
def test_agent_systemic_failure_halts_despite_max_failures(
    client, db_session, category
):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"], max_failures=100
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, status="failed", category=category, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    c = db_session.get(Campaign, cid)
    assert c.status == "stopped" and category in (c.halt_reason or "")
    assert _ch(db_session, cid, hs[1].id).job_id is None  # never exposed


def test_agent_concurrency_slot_held_until_proof(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [2],
        max_concurrency=1, observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)  # a fills the only slot
    assert _ch(db_session, cid, hs[0].id).state == "running"
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[1].id).state == "pending"  # still no proof
    _finish(db_session, cid, hs[0].id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "running"  # slot freed


def test_agent_reboot_budget_never_consumed(client, db_session):
    assert _consumes_reboot_budget("agent_upgrade", {"target_version": "0.15.0"}) is False
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [2],
        max_concurrency=2, observation_window_seconds=0,
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _reboot_budget_in_use(db_session) == 0
    assert _ch(db_session, cid, hs[0].id).awaited_boot is None
    assert _ch(db_session, cid, hs[1].id).awaited_boot is None


def test_agent_host_busy_stays_pending_and_retries(client, db_session):
    cid, (a,) = _running_agent(client, db_session, ["a"], ["rest"])
    db_session.add(Job(host_id=a.id, job_type="health_check", status="pending"))
    db_session.flush()
    advance_campaigns(now=NOW, db=db_session)
    ch = _ch(db_session, cid, a.id)
    assert ch.state == "pending" and ch.job_id is None
    assert _status(db_session, cid) == "running"
    other = db_session.execute(
        select(Job).where(Job.host_id == a.id, Job.campaign_id.is_(None))
    ).scalar_one()
    other.status = "succeeded"
    other.completed_at = NOW
    db_session.flush()
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    job = _job(db_session, cid, a.id)
    assert job.job_type == "agent_upgrade"
    assert _ch(db_session, cid, a.id).state == "running"


def test_agent_unclaimed_job_never_progresses_and_cancels_cleanly(
    client, db_session
):
    cid, (a,) = _running_agent(client, db_session, ["a"], ["rest"])
    advance_campaigns(now=NOW, db=db_session)
    assert _job(db_session, cid, a.id).status == "pending"  # never claimed
    advance_campaigns(now=NOW + timedelta(hours=1), db=db_session)
    assert _ch(db_session, cid, a.id).state == "running"  # not done, not skipped
    assert _status(db_session, cid) == "running"
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/cancel", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    assert _ch(db_session, cid, a.id).state == "orphaned"


def test_agent_pause_holds_everything_resume_reconciles(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/pause", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    _finish(db_session, cid, hs[0].id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "running"  # untouched paused
    assert _ch(db_session, cid, hs[1].id).state == "pending"
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/resume", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    advance_campaigns(now=NOW + timedelta(seconds=2), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "running"


@pytest.mark.parametrize("late_status", ["succeeded", "failed"])
def test_agent_cancel_late_result_resurrects_nothing(
    client, db_session, late_status
):
    cid, (a,) = _running_agent(client, db_session, ["a"], ["rest"])
    advance_campaigns(now=NOW, db=db_session)
    assert (
        client.post(
            f"/api/v1/admin/campaigns/{cid}/cancel", headers=ADMIN_HEADERS
        ).status_code
        == 200
    )
    assert _ch(db_session, cid, a.id).state == "orphaned"
    if late_status == "succeeded":
        _finish(db_session, cid, a.id, proven=True, at=NOW)
    else:
        _finish(
            db_session, cid, a.id, status="failed",
            category="upgrade_proof_timeout", at=NOW,
        )
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _status(db_session, cid) == "cancelled"
    assert _ch(db_session, cid, a.id).state == "orphaned"


def test_agent_restart_resumes_from_postgres_only(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=0
    )
    advance_campaigns(now=NOW, db=db_session)
    a_id, b_id = hs[0].id, hs[1].id
    _finish(db_session, cid, a_id, proven=True, at=NOW)
    db_session.expunge_all()  # whatever the process knew is gone
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, a_id).state == "done"
    assert _ch(db_session, cid, b_id).state == "running"


def test_agent_observation_gate_holds_stage_two(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b"], [1, "rest"], observation_window_seconds=60
    )
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "done"
    assert _ch(db_session, cid, hs[1].id).state == "pending"  # window open
    advance_campaigns(now=NOW + timedelta(seconds=61), db=db_session)
    assert _ch(db_session, cid, hs[1].id).state == "running"  # window elapsed


def test_agent_stages_roll_canary_then_rest(client, db_session):
    cid, hs = _running_agent(
        client, db_session, ["a", "b", "c"], [1, "rest"], observation_window_seconds=0
    )
    assert _ch(db_session, cid, hs[0].id).stage_index == 0
    assert _ch(db_session, cid, hs[1].id).stage_index == 1
    advance_campaigns(now=NOW, db=db_session)
    assert _ch(db_session, cid, hs[0].id).state == "running"
    assert _ch(db_session, cid, hs[1].id).job_id is None
    assert _ch(db_session, cid, hs[2].id).job_id is None
    _finish(db_session, cid, hs[0].id, proven=True, at=NOW)
    advance_campaigns(now=NOW + timedelta(seconds=1), db=db_session)
    assert _ch(db_session, cid, hs[1].id).state == "running"
    assert _ch(db_session, cid, hs[2].id).state == "pending"  # concurrency 1


def test_agent_corrupt_job_params_halts_without_creating(client, db_session):
    cid, (a,) = _running_agent(client, db_session, ["a"], ["rest"])
    db_session.get(Campaign, cid).job_params = {"url": "https://evil.invalid/x"}
    db_session.flush()
    advance_campaigns(now=NOW, db=db_session)
    c = db_session.get(Campaign, cid)
    assert c.status == "stopped" and "invalid job_params" in (c.halt_reason or "")
    assert _ch(db_session, cid, a.id).job_id is None
    assert (
        db_session.execute(select(Job).where(Job.campaign_id == cid)).first() is None
    )
