"""T0 robustness: crash / restart / concurrent-scheduler behaviour.

No functional change may come from these tests: a pass changes nothing, a
failure is documented, never silently fixed. Every concurrency scenario uses
real PostgreSQL transactions on independent connections (threads + barrier),
never mocks. Rows committed outside the rolled-back test transaction are
deleted at teardown.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.api.routes.jobs import submit_job_result
from app.campaigns.engine import advance_campaigns
from app.db.base import engine
from app.models.models import (
    Campaign,
    CampaignHost,
    Host,
    Job,
    Schedule,
    WebhookDelivery,
)
from app.scheduler import dispatch_pending_deliveries, reap_stuck_jobs, tick
from app.schemas.schemas import JobResultIn
from tests.conftest import ADMIN_HEADERS, webhook_row

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)


def _maker(**kw):
    opts = {"bind": engine, "autoflush": False, "expire_on_commit": False, "future": True}
    opts.update(kw)
    return sessionmaker(**opts)


def _t0_host(db, hostname: str) -> Host:
    host = Host(hostname=hostname, os_family="debian", package_manager="apt")
    db.add(host)
    db.flush()
    return host


def _t0_cleanup(*, host_ids=(), campaign_ids=(), hook_ids=()):
    db = _maker()()
    try:
        if hook_ids:
            db.query(WebhookDelivery).filter(
                WebhookDelivery.webhook_id.in_(hook_ids)
            ).delete(synchronize_session=False)
        for cid in campaign_ids:
            db.query(CampaignHost).filter(CampaignHost.campaign_id == cid).delete(
                synchronize_session=False
            )
            db.query(Campaign).filter(Campaign.id == cid).delete(
                synchronize_session=False
            )
        for hid in host_ids:
            db.query(Job).filter(Job.host_id == hid).delete(synchronize_session=False)
            db.query(Schedule).filter(Schedule.host_id == hid).delete(
                synchronize_session=False
            )
            db.query(Host).filter(Host.id == hid).delete(synchronize_session=False)
        from app.models.models import Webhook

        for wid in hook_ids:
            db.query(Webhook).filter(Webhook.id == wid).delete(
                synchronize_session=False
            )
        db.commit()
    finally:
        db.close()


def _t0_campaign(db, host_ids, *, max_concurrency=2, max_failures=3, name="t0"):
    campaign = Campaign(
        name=f"{name}-{uuid.uuid4().hex[:6]}",
        stages=[len(host_ids)],
        max_concurrency=max_concurrency,
        max_failures=max_failures,
        observation_window_seconds=600,
        status="running",
    )
    db.add(campaign)
    db.flush()
    for hid in host_ids:
        db.add(CampaignHost(campaign_id=campaign.id, host_id=hid, stage_index=0))
    db.flush()
    return campaign.id


def _run_pair(fn):
    """Run fn(db) on two independent sessions behind a barrier. Returns both."""
    maker = _maker()
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _one(tag: str) -> None:
        db = maker()
        try:
            barrier.wait(timeout=30)
            outcomes[tag] = fn(db)
        except Exception as exc:  # noqa: BLE001 -- reported, not hidden
            db.rollback()
            outcomes[tag] = exc
        finally:
            db.close()

    threads = [
        threading.Thread(target=_one, args=("a",), daemon=True),
        threading.Thread(target=_one, args=("b",), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert all(not thread.is_alive() for thread in threads), outcomes
    errors = {k: v for k, v in outcomes.items() if isinstance(v, Exception)}
    assert not errors, errors
    return outcomes["a"], outcomes["b"]


# --- 2 schedulers ------------------------------------------------------------


def test_t0_dual_tick_single_schedule_single_job(db_session):
    """Invariant: one due window yields one job, even with two ticks racing."""
    maker = _maker()
    setup = maker()
    try:
        host = _t0_host(setup, f"vm-t0-tick-{uuid.uuid4().hex[:6]}")
        host_id = host.id
        setup.add(
            Schedule(
                host_id=host_id,
                enabled=True,
                kind="weekly",
                weekday=0,
                hour=3,
                minute=0,
                timezone="UTC",
                params={},
                next_run_at=NOW - timedelta(minutes=1),
            )
        )
        setup.commit()
    finally:
        setup.close()

    try:
        _run_pair(lambda db: tick(now=NOW, db=db))
        check = maker()
        try:
            assert check.query(Job).filter(Job.host_id == host_id).count() == 1
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=[host_id])


def test_t0_dual_advance_no_double_job(db_session):
    """Invariant: two engines never create two jobs for one host or pass slots."""
    maker = _maker()
    setup = maker()
    try:
        hosts = [_t0_host(setup, f"vm-t0-adv-{i}-{uuid.uuid4().hex[:4]}") for i in range(3)]
        host_ids = [h.id for h in hosts]
        cid = _t0_campaign(setup, host_ids, max_concurrency=2, name="t0adv")
        setup.commit()
    finally:
        setup.close()

    try:
        _run_pair(lambda db: advance_campaigns(now=NOW, db=db))
        check = maker()
        try:
            jobs = check.query(Job).filter(Job.campaign_id == cid).all()
            assert len(jobs) == 2
            assert len({j.host_id for j in jobs}) == 2
            states = sorted(
                r.state
                for r in check.query(CampaignHost)
                .filter(CampaignHost.campaign_id == cid)
                .all()
            )
            assert states == ["pending", "running", "running"]
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=host_ids, campaign_ids=[cid])


def test_t0_dual_reaper_single_transition(db_session):
    """Invariant: one stuck job fails once with one reaped event."""
    maker = _maker()
    setup = maker()
    try:
        host = _t0_host(setup, f"vm-t0-reap-{uuid.uuid4().hex[:6]}")
        host.last_seen_at = NOW - timedelta(minutes=1)
        host_id = host.id
        job = Job(
            host_id=host_id,
            job_type="apt_upgrade",
            status="running",
            started_at=NOW - timedelta(hours=5),
        )
        setup.add(job)
        setup.flush()
        job_id = job.id
        hook_id = webhook_row(setup, events=("job.failed",)).id
        setup.commit()
    finally:
        setup.close()

    try:
        _run_pair(lambda db: reap_stuck_jobs(now=NOW, db=db, timeout_seconds=7200))
        check = maker()
        try:
            stored = check.get(Job, job_id)
            assert stored.status == "failed"
            assert stored.failure_category == "timeout"
            deliveries = [
                d
                for d in check.query(WebhookDelivery).all()
                if (d.payload.get("data") or {}).get("job_id") == str(job_id)
            ]
            assert len(deliveries) == 1
            assert deliveries[0].payload["data"]["reaped"] is True
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=[host_id], hook_ids=[hook_id])


def test_t0_dual_dispatch_single_post(db_session):
    """Invariant: two dispatchers never POST the same delivery twice.

    A redelivery of the same delivery_id after a crash-before-commit stays
    legal at-least-once; that is a separate scenario, not asserted here.
    """
    posts: list[tuple[str, str]] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            posts.append(
                (self.path, self.headers.get("X-Cadence-Delivery", ""))
            )
            if len(posts) == 1:
                import time as _time

                _time.sleep(2)  # hold the row lock while the peer arrives
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: ANN001, ANN202
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    maker = _maker()
    setup = maker()
    try:
        hook_id = webhook_row(
            setup,
            url=f"http://127.0.0.1:{port}/hook",
            events=("job.succeeded",),
        ).id
        setup.add(
            WebhookDelivery(
                webhook_id=hook_id,
                event_type="job.succeeded",
                payload={"event_type": "job.succeeded", "data": {"job_id": "t0"}},
                status="pending",
                attempt_count=0,
                next_attempt_at=NOW - timedelta(seconds=1),
            )
        )
        setup.commit()
    finally:
        setup.close()

    try:
        _run_pair(lambda db: dispatch_pending_deliveries(now=NOW, db=db))
        assert len(posts) == 1
        assert posts[0][1] != ""
        check = maker()
        try:
            remaining = (
                check.query(WebhookDelivery)
                .filter(WebhookDelivery.webhook_id == hook_id)
                .all()
            )
            assert len(remaining) == 1
            assert remaining[0].status == "delivered"
            assert str(remaining[0].id) == posts[0][1]
        finally:
            check.close()
    finally:
        server.shutdown()
        _t0_cleanup(hook_ids=[hook_id])


# --- restart / recovery ------------------------------------------------------


def test_t0_restart_mid_campaign_resumes_from_pg(db_session):
    """Invariant: after a restart (fresh session, no memory) the engine
    resumes from committed rows; no host is ever half-advanced."""
    maker = _maker()
    setup = maker()
    try:
        hosts = [_t0_host(setup, f"vm-t0-rst-{i}-{uuid.uuid4().hex[:4]}") for i in range(2)]
        host_ids = [h.id for h in hosts]
        cid = _t0_campaign(setup, host_ids, max_concurrency=2, name="t0rst")
        setup.commit()
    finally:
        setup.close()

    valid = {"pending", "running", "done", "skipped", "orphaned"}

    def _snapshot():
        db = maker()
        try:
            rows = (
                db.query(CampaignHost).filter(CampaignHost.campaign_id == cid).all()
            )
            return (
                db.get(Campaign, cid).status,
                sorted(r.state for r in rows),
                [r.job_id for r in rows if r.state == "running"],
            )
        finally:
            db.close()

    try:
        advance_campaigns(now=NOW, db=maker())  # pass 1: jobs created
        status, states, _ = _snapshot()
        assert status == "running" and states == ["running", "running"]

        # "Crash": drop every session. Finish one job on a new connection.
        fin = maker()
        try:
            row = fin.execute(
                select(CampaignHost).where(
                    CampaignHost.campaign_id == cid, CampaignHost.host_id == host_ids[0]
                )
            ).scalar_one()
            job = fin.get(Job, row.job_id)
            job.status = "succeeded"
            job.completed_at = NOW
            job.result = {"health_status": "healthy"}
            fin.commit()
        finally:
            fin.close()

        advance_campaigns(now=NOW, db=maker())  # resume from PG only
        status, states, _ = _snapshot()
        assert status == "running"
        assert set(states) <= valid
        assert states == ["done", "running"]

        fin = maker()
        try:
            row = fin.execute(
                select(CampaignHost).where(
                    CampaignHost.campaign_id == cid, CampaignHost.host_id == host_ids[1]
                )
            ).scalar_one()
            job = fin.get(Job, row.job_id)
            job.status = "succeeded"
            job.completed_at = NOW
            job.result = {"health_status": "healthy"}
            fin.commit()
        finally:
            fin.close()

        advance_campaigns(now=NOW + timedelta(hours=2), db=maker())
        status, states, _ = _snapshot()
        assert status == "completed"
        assert states == ["done", "done"]
    finally:
        _t0_cleanup(host_ids=host_ids, campaign_ids=[cid])


def test_t0_late_result_after_reaper_is_inert(db_session):
    """Invariant: a result arriving after the reaper changes nothing."""
    maker = _maker()
    setup = maker()
    try:
        host = _t0_host(setup, f"vm-t0-late-{uuid.uuid4().hex[:6]}")
        host.last_seen_at = NOW - timedelta(minutes=1)
        host_id = host.id
        job = Job(
            host_id=host_id,
            job_type="apt_upgrade",
            status="running",
            started_at=NOW - timedelta(hours=5),
        )
        setup.add(job)
        setup.flush()
        job_id = job.id
        setup.commit()
    finally:
        setup.close()

    try:
        work = maker()
        try:
            assert reap_stuck_jobs(now=NOW, db=work, timeout_seconds=7200) == 1
            work.commit()
            before = {
                "status": work.get(Job, job_id).status,
                "result": dict(work.get(Job, job_id).result or {}),
                "category": work.get(Job, job_id).failure_category,
            }
            host_before = (
                work.get(Host, host_id).health_status,
                work.get(Host, host_id).health_checked_at,
            )
            deliveries_before = work.query(WebhookDelivery).count()
        finally:
            work.close()

        late = maker()
        try:
            host_obj = late.get(Host, host_id)
            import fastapi

            try:
                submit_job_result(
                    job_id,
                    JobResultIn(status="succeeded", exit_code=0, log="late"),
                    host_obj,
                    late,
                )
                late_status = 200
            except fastapi.HTTPException as exc:
                late.rollback()
                late_status = exc.status_code
            assert late_status == 409
        finally:
            late.close()

        check = maker()
        try:
            stored = check.get(Job, job_id)
            assert stored.status == before["status"] == "failed"
            assert dict(stored.result or {}) == before["result"]
            assert stored.failure_category == before["category"]
            assert (
                check.get(Host, host_id).health_status,
                check.get(Host, host_id).health_checked_at,
            ) == host_before
            assert check.query(WebhookDelivery).count() == deliveries_before
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=[host_id])


# --- pause / resume / cancel -------------------------------------------------


def test_t0_pause_resume_cancel_repeated(client, db_session):
    """Invariant: terminal campaigns never reopen; illegal moves are 409."""
    from tests.test_campaigns_engine import _running

    cid, _ = _running(client, db_session, ["t0p1"], [1])
    for _ in range(10):
        assert (
            client.post(f"/api/v1/admin/campaigns/{cid}/pause", headers=ADMIN_HEADERS).status_code
            == 200
        )
        assert (
            client.post(f"/api/v1/admin/campaigns/{cid}/resume", headers=ADMIN_HEADERS).status_code
            == 200
        )
    assert (
        client.post(f"/api/v1/admin/campaigns/{cid}/cancel", headers=ADMIN_HEADERS).status_code
        == 200
    )
    assert db_session.get(Campaign, cid).status == "cancelled"
    for verb in ("pause", "resume", "cancel"):
        assert (
            client.post(f"/api/v1/admin/campaigns/{cid}/{verb}", headers=ADMIN_HEADERS).status_code
            == 409
        )
    assert db_session.get(Campaign, cid).status == "cancelled"


def test_t0_cancel_with_running_jobs_no_resurrection(client, db_session):
    """Invariant: cancelling with jobs in flight ends the campaign for good."""
    from tests.test_campaigns_engine import _ch, _running

    cid, _ = _running(client, db_session, ["t0cancel-x"], [1])
    # One campaign host with a real running job behind it.
    hs = db_session.query(Host).filter(Host.hostname == "t0cancel-x").one()
    advance_campaigns(now=NOW, db=db_session)
    assert _ch(db_session, cid, hs.id).state == "running"

    assert (
        client.post(f"/api/v1/admin/campaigns/{cid}/cancel", headers=ADMIN_HEADERS).status_code
        == 200
    )
    # The orphaned in-flight job still completes on the agent path...
    ch = _ch(db_session, cid, hs.id)
    assert ch.state == "orphaned"
    # ...without reopening or failing the campaign.
    advance_campaigns(now=NOW, db=db_session)
    assert db_session.get(Campaign, cid).status == "cancelled"
    assert _ch(db_session, cid, hs.id).state == "orphaned"


# --- failure budget / max_concurrency ----------------------------------------


def test_t0_failure_budget_simultaneous_skips_halt(client, db_session):
    """Invariant: a breached skip budget stops the campaign, same tick or not."""

    maker = _maker()

    def _failed_pair_same_tick():
        # Committed setup on its own connection: the rolled-back fixture's
        # outer transaction is never visible to the racing sessions.
        suffix = uuid.uuid4().hex[:6]
        setup = maker()
        try:
            hosts = [_t0_host(setup, f"vm-t0-fb-{suffix}-{i}") for i in range(2)]
            host_ids = [h.id for h in hosts]
            cid = _t0_campaign(
                setup, host_ids, max_concurrency=2, max_failures=1, name="t0fb"
            )
            setup.commit()
        finally:
            setup.close()
        first = maker()
        try:
            advance_campaigns(now=NOW, db=first)
        finally:
            first.close()
        barrier = threading.Barrier(2)
        errors: dict[str, object] = {}

        def _fail(tag, hid, category):
            own = maker()
            try:
                barrier.wait(timeout=30)
                job_id = own.execute(
                    select(CampaignHost.job_id).where(
                        CampaignHost.campaign_id == cid, CampaignHost.host_id == hid
                    )
                ).scalar_one()
                job = own.get(Job, job_id)
                job.status = "failed"
                job.failure_category = category
                job.completed_at = NOW
                own.commit()
            except Exception as exc:  # noqa: BLE001 -- reported, not hidden
                own.rollback()
                errors[tag] = exc
            finally:
                own.close()

        try:
            threads = [
                threading.Thread(target=_fail, args=("a", host_ids[0], "timeout"), daemon=True),
                threading.Thread(target=_fail, args=("b", host_ids[1], "dpkg_error"), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            assert all(not thread.is_alive() for thread in threads)
            assert not errors, errors
            last = maker()
            try:
                advance_campaigns(now=NOW, db=last)
            finally:
                last.close()
            check = maker()
            try:
                campaign = check.get(Campaign, cid)
                assert campaign.status == "stopped"
                assert campaign.halt_reason == "max_failures exceeded"
            finally:
                check.close()
        finally:
            _t0_cleanup(host_ids=host_ids, campaign_ids=[cid])

    _failed_pair_same_tick()


def test_t0_failure_budget_halt_category_stops(client, db_session):
    """Invariant: a halt-category failure stops the campaign exactly once."""
    from tests.test_campaigns_engine import _finish, _running

    cid, hs = _running(client, db_session, ["t0h1", "t0h2"], [2], max_concurrency=2,
                       max_failures=5)
    advance_campaigns(now=NOW, db=db_session)
    _finish(db_session, cid, hs[0].id, status="failed", category="network_or_repo")
    _finish(db_session, cid, hs[1].id, status="failed", category="network_or_repo")
    advance_campaigns(now=NOW, db=db_session)
    campaign = db_session.get(Campaign, cid)
    assert campaign.status == "stopped"
    assert campaign.halt_reason is not None and "network_or_repo" in campaign.halt_reason


def test_t0_max_concurrency_under_two_schedulers(db_session):
    """Invariant: two engines never fill past max_concurrency."""
    maker = _maker()
    setup = maker()
    try:
        hosts = [_t0_host(setup, f"vm-t0-mc-{i}-{uuid.uuid4().hex[:4]}") for i in range(3)]
        host_ids = [h.id for h in hosts]
        cid = _t0_campaign(setup, host_ids, max_concurrency=1, name="t0mc")
        setup.commit()
    finally:
        setup.close()

    try:
        _run_pair(lambda db: advance_campaigns(now=NOW, db=db))
        check = maker()
        try:
            jobs = check.query(Job).filter(Job.campaign_id == cid).all()
            assert len(jobs) == 1
        finally:
            check.close()
    finally:
        _t0_cleanup(host_ids=host_ids, campaign_ids=[cid])
