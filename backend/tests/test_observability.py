import logging

from app.core.logging import LogfmtFormatter


def test_readyz_checks_the_database(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ready"}


def test_healthz_still_liveness_only(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_request_id_header_generated_and_echoed(client):
    r = client.get("/healthz")
    assert len(r.headers.get("x-request-id", "")) >= 8

    r = client.get("/healthz", headers={"X-Request-ID": "abc123"})
    assert r.headers["x-request-id"] == "abc123"


def test_request_is_logged_with_fields(client, caplog):
    with caplog.at_level(logging.INFO, logger="cadence.request"):
        client.get("/healthz")
    rec = next(r for r in caplog.records if r.name == "cadence.request")
    f = rec.fields
    assert f["method"] == "GET"
    assert f["path"] == "/healthz"
    assert f["status"] == 200
    assert "duration_ms" in f and "request_id" in f


def test_logfmt_formatter_quotes_and_appends_fields():
    fmt = LogfmtFormatter()
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello world", None, None)
    rec.fields = {"n": 3, "s": "a b"}
    out = fmt.format(rec)
    assert 'msg="hello world"' in out
    assert "n=3" in out
    assert 's="a b"' in out
    assert out.startswith("ts=") and " level=info " in out
