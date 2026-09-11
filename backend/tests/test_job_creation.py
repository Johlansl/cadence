"""app.job_creation.create_job_for_host -- the shared path the admin route, the
scheduler and (later) the campaign engine all create jobs through.

The route- and scheduler-level behaviour is already covered by test_admin.py,
test_exclusion_resolution.py and test_scheduler.py; these tests pin the
function's own contract: the None-on-conflict return, the per-job-type
injection, and the campaign_id wiring nothing else exercises yet.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.job_creation import create_job_for_host
from app.models.models import (
    Campaign,
    Host,
    HostPackage,
    Job,
    Package,
    PackageExclusion,
)


def _health_checks() -> dict:
    return {
        "minimum_available_bytes": settings.upgrade_minimum_available_bytes,
        "boot_minimum_available_bytes": settings.upgrade_boot_minimum_available_bytes,
        "lock_wait_seconds": settings.upgrade_lock_wait_seconds,
    }


def _host(db_session, hostname: str = "vm-jc") -> Host:
    h = Host(hostname=hostname)
    db_session.add(h)
    db_session.flush()
    return h


def test_apt_upgrade_injects_both_held_lists_and_flushes_an_id(db_session):
    host = _host(db_session)

    job = create_job_for_host(db_session, host_id=host.id)

    assert job is not None
    assert job.id is not None
    assert job.job_type == "apt_upgrade"
    assert job.status == "pending"
    assert job.params == {
        "excluded_packages": [],
        "known_held_packages": [],
        "health_checks": _health_checks(),
    }
    assert job.requested_by is None
    assert job.campaign_id is None


def test_dry_run_gets_excluded_but_not_known_held(db_session):
    host = _host(db_session)

    job = create_job_for_host(db_session, host_id=host.id, job_type="apt_dry_run")

    assert job.params == {"excluded_packages": []}
    assert "known_held_packages" not in job.params


def test_reboot_job_gets_neither_held_list(db_session):
    host = _host(db_session)

    job = create_job_for_host(db_session, host_id=host.id, job_type="reboot")

    assert job.params == {}


def test_health_check_job_gets_thresholds_but_no_excluded_or_held_lists(db_session):
    host = _host(db_session)

    job = create_job_for_host(db_session, host_id=host.id, job_type="health_check")

    assert job.params == {"health_checks": _health_checks()}
    assert "excluded_packages" not in job.params
    assert "known_held_packages" not in job.params


def test_caller_params_are_kept_but_server_values_are_overwritten(db_session):
    host = _host(db_session)

    job = create_job_for_host(
        db_session,
        host_id=host.id,
        params={
            "reboot": "auto",
            "excluded_packages": ["made-up"],
            "health_checks": {"lock_wait_seconds": 1},
        },
    )

    assert job.params["reboot"] == "auto"
    assert job.params["excluded_packages"] == []  # server-resolved, caller ignored
    assert job.params["health_checks"] == _health_checks()


def test_excluded_packages_reflects_a_matching_exclusion_rule(db_session):
    host = _host(db_session)
    pkg = Package(name="docker-ce", architecture="amd64")
    db_session.add(pkg)
    db_session.flush()
    db_session.add(
        HostPackage(host_id=host.id, package_id=pkg.id, installed_version="1.0")
    )
    db_session.add(PackageExclusion(scope="global", pattern="docker-*"))
    db_session.flush()

    job = create_job_for_host(db_session, host_id=host.id)

    assert job.params["excluded_packages"] == ["docker-ce"]


@pytest.mark.parametrize("active_status", ["pending", "running"])
def test_returns_none_when_the_host_has_an_active_job(db_session, active_status):
    host = _host(db_session)
    db_session.add(Job(host_id=host.id, job_type="apt_upgrade", status=active_status))
    db_session.flush()

    assert create_job_for_host(db_session, host_id=host.id) is None


@pytest.mark.parametrize("done_status", ["succeeded", "failed"])
def test_a_terminal_prior_job_does_not_block(db_session, done_status):
    host = _host(db_session)
    db_session.add(Job(host_id=host.id, job_type="apt_upgrade", status=done_status))
    db_session.flush()

    job = create_job_for_host(db_session, host_id=host.id)

    assert job is not None


def test_campaign_id_and_requested_by_are_written_through(db_session):
    host = _host(db_session)
    campaign = Campaign(
        name="c",
        stages=[1],
        max_concurrency=1,
        max_failures=0,
        observation_window_seconds=600,
    )
    db_session.add(campaign)
    db_session.flush()

    job = create_job_for_host(
        db_session,
        host_id=host.id,
        requested_by="campaign",
        campaign_id=campaign.id,
    )

    assert job.campaign_id == campaign.id
    assert job.requested_by == "campaign"
