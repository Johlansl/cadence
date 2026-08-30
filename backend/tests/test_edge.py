from sqlalchemy import func, select

from app.models.models import HostPackage, Package, Report
from tests.conftest import ADMIN_HEADERS, bearer, create_host, pkg, report_payload


def test_raw_payload_keeps_unknown_fields(client, db_session):
    host_id, token = create_host(client)
    payload = report_payload(packages=[pkg("bash")])
    payload["experimental_flag"] = True
    payload["packages"][0]["vendor_note"] = "kept"

    r = client.post("/api/v1/reports", headers=bearer(token), json=payload)
    assert r.status_code == 200

    row = db_session.execute(
        select(Report).where(Report.host_id == host_id)
    ).scalars().one()
    assert row.raw_payload["experimental_flag"] is True
    assert row.raw_payload["packages"][0]["vendor_note"] == "kept"


def test_packages_dimension_is_shared_across_hosts(client, db_session):
    h1, t1 = create_host(client, hostname="a")
    h2, t2 = create_host(client, hostname="b")
    common = pkg("openssl", candidate="3.1")

    client.post("/api/v1/reports", headers=bearer(t1), json=report_payload(packages=[common]))
    client.post("/api/v1/reports", headers=bearer(t2), json=report_payload(packages=[common]))

    n_pkg = db_session.execute(
        select(func.count()).select_from(Package).where(Package.name == "openssl")
    ).scalar_one()
    assert n_pkg == 1

    pkg_id = db_session.execute(
        select(Package.id).where(Package.name == "openssl")
    ).scalar_one()
    host_pkg_ids = {
        str(x)
        for x in db_session.execute(
            select(HostPackage.host_id).where(HostPackage.package_id == pkg_id)
        ).scalars()
    }
    assert host_pkg_ids == {h1, h2}


def test_report_piggyback_and_poll_do_not_double_claim(client, db_session):
    host_id, token = create_host(client)
    client.post(f"/api/v1/admin/hosts/{host_id}/jobs", headers=ADMIN_HEADERS, json={})

    first = client.post("/api/v1/agent/next-job", headers=bearer(token)).json()["job"]
    assert first is not None

    # A report right after must not hand the same (now running) job out again.
    second = client.post(
        "/api/v1/reports", headers=bearer(token), json=report_payload()
    ).json()["job"]
    assert second is None
