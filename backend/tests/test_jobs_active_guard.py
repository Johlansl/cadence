"""One active job per host, enforced by PostgreSQL (migration 0023).

The application-level check in create_job_for_host can lose a race; the
partial unique index is the final authority and the helper maps a
concurrent collision back to None ("host busy"). These tests use real
PostgreSQL transactions, never mocks. Rows committed on their own
connections are deleted at teardown.
"""

from __future__ import annotations

import pathlib
import threading
import time
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db.base import engine
from app.job_creation import create_job_for_host
from app.models.models import Host, Job

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]


def _maker(*, autoflush: bool = False):
    return sessionmaker(
        bind=engine, autoflush=autoflush, expire_on_commit=False, future=True
    )


def _setup_host_with_hookless_job():
    """A committed host with no jobs. Returns (host_id, maker)."""
    maker = _maker()
    setup = maker()
    try:
        host = Host(
            hostname=f"vm-r1-{uuid.uuid4().hex[:8]}",
            os_family="debian",
            package_manager="apt",
        )
        setup.add(host)
        setup.flush()
        host_id = host.id
        setup.commit()
        return host_id
    finally:
        setup.close()


def _cleanup(host_id):
    cleanup = _maker()()
    try:
        cleanup.query(Job).filter(Job.host_id == host_id).delete()
        cleanup.query(Host).filter(Host.id == host_id).delete()
        cleanup.commit()
    finally:
        cleanup.close()


def test_concurrent_create_yields_exactly_one_job():
    host_id = _setup_host_with_hookless_job()
    maker = _maker()
    barrier = threading.Barrier(4)
    outcomes: dict[str, object] = {}

    def _create(tag: str) -> None:
        db = maker()
        try:
            barrier.wait(timeout=30)
            outcomes[tag] = create_job_for_host(db, host_id=host_id)
            db.commit()
        finally:
            db.close()

    try:
        threads = [threading.Thread(target=_create, args=(str(i),), daemon=True) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert all(not thread.is_alive() for thread in threads), outcomes
        created = [job for job in outcomes.values() if job is not None]
        assert len(created) == 1
        assert sum(1 for job in outcomes.values() if job is None) == 3

        check = maker()
        try:
            assert check.query(Job).filter(Job.host_id == host_id).count() == 1
        finally:
            check.close()
    finally:
        _cleanup(host_id)


def test_direct_insert_bypassing_the_helper_is_rejected(db_session):
    host = Host(hostname=f"vm-r1-{uuid.uuid4().hex[:8]}")
    db_session.add(host)
    db_session.flush()

    first = create_job_for_host(db_session, host_id=host.id)
    assert first is not None
    db_session.flush()

    with pytest.raises(IntegrityError) as excinfo:
        with db_session.begin_nested():
            db_session.add(Job(host_id=host.id, job_type="reboot", status="pending"))
            db_session.flush()
    orig = excinfo.value.orig
    assert getattr(orig, "pgcode", None) == "23505"
    # Ground truth for the classifier: PostgreSQL names our index.
    assert orig.diag.constraint_name == "ux_jobs_one_active_per_host"

    # A future caller that forgets the guard fails loudly instead of
    # silently breaking the invariant; terminal rows stay unlimited.
    db_session.add(Job(host_id=host.id, job_type="reboot", status="succeeded"))
    db_session.add(Job(host_id=host.id, job_type="reboot", status="failed"))
    db_session.flush()


def _integrity_error(*, pgcode, constraint_name="ux_jobs_one_active_per_host"):
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    orig = SimpleNamespace(pgcode=pgcode, diag=SimpleNamespace(constraint_name=constraint_name))
    return IntegrityError("INSERT INTO jobs", {}, orig)


def test_collision_classifier_accepts_only_our_index():
    from app.job_creation import _is_active_job_collision

    assert _is_active_job_collision(_integrity_error(pgcode="23505")) is True
    # A different unique violation on the same flush must propagate.
    assert (
        _is_active_job_collision(
            _integrity_error(pgcode="23505", constraint_name="jobs_pkey")
        )
        is False
    )
    assert (
        _is_active_job_collision(
            _integrity_error(pgcode="23503", constraint_name=None)
        )
        is False
    )
    # Unnamed diagnostics propagate too: only the exact index name reads busy.
    assert (
        _is_active_job_collision(
            _integrity_error(pgcode="23505", constraint_name=None)
        )
        is False
    )


@pytest.mark.parametrize("autoflush", [True, False])
def test_collision_preserves_the_caller_transaction(autoflush):
    """A lost race must not eat the caller's own pending work.

    S1 holds an uncommitted active job (invisible to S2's check, so S2
    reaches the INSERT and blocks on it). S2 dirties its host row first;
    after S1 commits, S2's helper returns None and S2 can still commit its
    own change on the same transaction.

    autoflush=True (production shape): the helper's first SELECT already
    flushes the caller work outside the savepoint, exactly as before R1.
    autoflush=False: begin_nested() flushes it unconditionally just before
    the savepoint (SQLAlchemy-documented, confirmed by traced flushes), so
    the savepoint rollback only ever undoes the helper's own INSERT. Both
    must preserve the caller change.
    """
    host_id = _setup_host_with_hookless_job()
    maker = _maker(autoflush=autoflush)
    new_hostname = f"vm-r1-renamed-{uuid.uuid4().hex[:8]}"
    s1_ready = threading.Event()
    outcomes: dict[str, object] = {}

    s1 = maker()
    try:
        s1.add(Job(host_id=host_id, job_type="apt_upgrade", status="pending"))
        s1.flush()  # uncommitted: S2's check cannot see it yet
        s1_ready.set()

        def _loser() -> None:
            db = maker()
            try:
                s1_ready.wait(timeout=30)
                own_host = db.get(Host, host_id)
                own_host.hostname = new_hostname  # caller work, pending
                time.sleep(3)  # let S1 stay uncommitted while we reach INSERT
                outcomes["job"] = create_job_for_host(db, host_id=host_id)
                db.commit()  # the session must still be usable
                outcomes["committed"] = True
            except Exception as exc:  # noqa: BLE001 -- reported, not hidden
                outcomes["error"] = repr(exc)
            finally:
                db.close()

        thread = threading.Thread(target=_loser, daemon=True)
        thread.start()
        time.sleep(6)  # S2 is blocked in INSERT (or still arriving); commit S1
        s1.commit()
        thread.join(timeout=60)
        assert not thread.is_alive(), outcomes
        assert outcomes.get("error") is None, outcomes
        assert outcomes.get("job") is None, outcomes  # busy, not an error
        assert outcomes.get("committed") is True, outcomes

        check = maker()
        try:
            assert check.get(Host, host_id).hostname == new_hostname
            assert check.query(Job).filter(Job.host_id == host_id).count() == 1
        finally:
            check.close()
    finally:
        s1.close()
        _cleanup(host_id)


# --- migration 0023 up / down -------------------------------------------------


@pytest.fixture()
def throwaway_db(monkeypatch):
    """A brand-new empty database, dropped on teardown."""
    from app.core.config import settings

    base, _, _ = settings.database_url.rpartition("/")
    name = "cadence_mig0023_test"
    admin = create_engine(f"{base}/postgres", isolation_level="AUTOCOMMIT", future=True)
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = f"{base}/{name}"
    monkeypatch.setattr(settings, "database_url", url)
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _alembic_config():
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


def _has_active_index(url: str) -> bool:
    eng = create_engine(url, future=True)
    try:
        with eng.connect() as conn:
            return "ux_jobs_one_active_per_host" in [
                index["name"] for index in inspect(conn).get_indexes("jobs")
            ]
    finally:
        eng.dispose()


def test_migration_up_down_up_without_duplicates(throwaway_db):
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "0023")
    assert _has_active_index(throwaway_db)

    command.downgrade(cfg, "0022")
    assert not _has_active_index(throwaway_db)

    command.upgrade(cfg, "0023")
    assert _has_active_index(throwaway_db)


def test_migration_refuses_with_duplicate_diagnostic(throwaway_db):
    from alembic import command

    cfg = _alembic_config()
    command.upgrade(cfg, "0022")

    eng = create_engine(throwaway_db, future=True)
    try:
        with eng.begin() as conn:
            host_id = conn.execute(
                text("INSERT INTO hosts (hostname) VALUES ('vm-dup') RETURNING id")
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO jobs (host_id, job_type, status) "
                    "VALUES (:h, 'apt_upgrade', 'pending'), (:h, 'reboot', 'running')"
                ),
                {"h": str(host_id)},
            )
    finally:
        eng.dispose()

    with pytest.raises(Exception, match="migration 0023 refused"):
        command.upgrade(cfg, "0023")
    assert not _has_active_index(throwaway_db)
