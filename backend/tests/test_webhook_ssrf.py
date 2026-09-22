"""Unit tests for the webhook SSRF guard (app.webhooks.ssrf) and its wiring
into the dispatcher. All network access is stubbed: DNS via faked
socket.getaddrinfo answers and TCP/TLS via faked connection classes, so
no test touches the real network."""

from __future__ import annotations

import logging
import socket

import pytest

from app.core.config import settings
from app.webhooks import ssrf
from app.webhooks.delivery import dispatch_pending_deliveries
from app.webhooks.enqueue import enqueue_event
from app.webhooks.ssrf import SSRFBlocked, post_guarded
from tests.conftest import webhook_row

BODY = b'{"k":"v"}'
HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": "cadence-webhook",
    "Authorization": "Bearer secret",
    "X-Cadence-Signature": "sig",
    "X-Cadence-Event": "job.succeeded",
}
PUBLIC_IP = "93.184.216.34"
PUBLIC_V6 = "2606:4700:4700::1111"


def _v4(ip: str, port: int = 443):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


def _v6(ip: str, port: int = 443):
    return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0))]


class _Resp:
    def __init__(self, status: int, location: str | None = None):
        self.status = status
        self._location = location

    def getheader(self, name: str, default=None):
        if name.lower() == "location":
            return self._location if self._location is not None else default
        return default

    def read(self, n: int = -1):
        return b""


class _FakeConn:
    """Stand-in for the pinned connection classes. Records what the guard
    dialed (host_arg = validated IP) versus what it presented (real_host,
    Host header). Behaviour is driven by the per-test handler."""

    instances: list["_FakeConn"] = []
    handler = None

    def __init__(self, host, port=None, timeout=None, real_host=None, **kwargs):
        self.host_arg = host
        self.port_arg = port
        self.real_host = real_host
        self.method = None
        self.path = None
        self.request_headers: dict = {}
        type(self).instances.append(self)

    def request(self, method, path, body=None, headers=None):
        self.method = method
        self.path = path
        self.body = body
        self.request_headers = dict(headers or {})

    def getresponse(self):
        assert type(self).handler is not None, "test must set _FakeConn.handler"
        return type(self).handler(self)

    def close(self):
        pass


@pytest.fixture()
def fake_conns(monkeypatch):
    _FakeConn.instances = []
    _FakeConn.handler = None
    monkeypatch.setattr(ssrf, "_HTTP_CONN_CLS", _FakeConn)
    monkeypatch.setattr(ssrf, "_HTTPS_CONN_CLS", _FakeConn)
    return _FakeConn


def _dns(monkeypatch, mapping: dict, calls: list | None = None):
    """Stub getaddrinfo. mapping: hostname -> list of (family, ip)."""

    def fake(host, port, type=None, *args, **kwargs):
        if calls is not None:
            calls.append(host)
        if isinstance(mapping, dict):
            entries = mapping[host]
        else:  # pragma: no cover -- scripted sequence form
            entries = mapping(host, port)
        out = []
        for family, ip in entries:
            if family == socket.AF_INET6:
                out.append((family, socket.SOCK_STREAM, 6, "", (ip, port, 0, 0)))
            else:
                out.append((family, socket.SOCK_STREAM, 6, "", (ip, port)))
        return out

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)


def _no_dns(monkeypatch):
    def boom(host, port, *args, **kwargs):
        raise AssertionError(f"DNS must not be consulted for {host!r}")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", boom)


def _no_conn(monkeypatch):
    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("must not connect when the URL is refused")

    monkeypatch.setattr(ssrf, "_HTTP_CONN_CLS", _Boom)
    monkeypatch.setattr(ssrf, "_HTTPS_CONN_CLS", _Boom)


# --- literal blocked addresses ---------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://10.0.0.5/hook",
        "http://192.168.1.1:8080/hook",
        "http://172.16.0.2/hook",
    ],
)
def test_literal_private_ipv4_blocked_without_dns(monkeypatch, url):
    _no_dns(monkeypatch)
    _no_conn(monkeypatch)
    with pytest.raises(SSRFBlocked):
        post_guarded(url, BODY, HEADERS, 5.0)


@pytest.mark.parametrize("url", ["http://0.0.0.0/hook", "http://169.254.169.254/latest/"])
def test_unspecified_and_metadata_blocked(monkeypatch, url):
    _no_dns(monkeypatch)
    _no_conn(monkeypatch)
    with pytest.raises(SSRFBlocked, match="(?i)(unspecified|link-local|private|non-public)"):
        post_guarded(url, BODY, HEADERS, 5.0)


@pytest.mark.parametrize("url", ["http://[::1]/hook", "http://[::1]:8080/hook"])
def test_loopback_ipv6_literal_blocked(monkeypatch, url):
    _no_dns(monkeypatch)
    _no_conn(monkeypatch)
    with pytest.raises(SSRFBlocked):
        post_guarded(url, BODY, HEADERS, 5.0)


def test_hostname_resolving_to_private_blocked_before_connect(monkeypatch, fake_conns):
    _dns(monkeypatch, {"internal.example.com": [(socket.AF_INET, "10.1.2.3")]})
    with pytest.raises(SSRFBlocked):
        post_guarded("http://internal.example.com/hook", BODY, HEADERS, 5.0)
    assert fake_conns.instances == []


def test_mixed_public_and_private_answers_fail_closed(monkeypatch, fake_conns):
    _dns(
        monkeypatch,
        {"mixed.example.com": [(socket.AF_INET, PUBLIC_IP), (socket.AF_INET, "192.168.0.1")]},
    )
    with pytest.raises(SSRFBlocked):
        post_guarded("https://mixed.example.com/hook", BODY, HEADERS, 5.0)
    assert fake_conns.instances == []


def test_userinfo_refused(monkeypatch, fake_conns):
    _dns(monkeypatch, {"example.com": [(socket.AF_INET, PUBLIC_IP)]})
    with pytest.raises(SSRFBlocked, match="userinfo"):
        post_guarded("http://user:pass@example.com/hook", BODY, HEADERS, 5.0)
    assert fake_conns.instances == []


def test_non_http_scheme_refused(monkeypatch, fake_conns):
    with pytest.raises(SSRFBlocked, match="scheme"):
        post_guarded("ftp://example.com/hook", BODY, HEADERS, 5.0)
    assert fake_conns.instances == []


# --- redirects --------------------------------------------------------------


def test_redirect_to_private_blocked(monkeypatch, fake_conns):
    _dns(monkeypatch, {"public.example.test": [(socket.AF_INET, PUBLIC_IP)]})

    def handler(conn):
        assert conn.real_host == "public.example.test"
        return _Resp(302, "http://10.0.0.1/x")

    fake_conns.handler = handler
    with pytest.raises(SSRFBlocked):
        post_guarded("https://public.example.test/hook", BODY, HEADERS, 5.0)
    assert len(fake_conns.instances) == 1


def test_redirect_to_ftp_refused(monkeypatch, fake_conns):
    _dns(monkeypatch, {"public.example.test": [(socket.AF_INET, PUBLIC_IP)]})
    fake_conns.handler = lambda conn: _Resp(302, "ftp://example.com/x")
    with pytest.raises(SSRFBlocked, match="scheme"):
        post_guarded("https://public.example.test/hook", BODY, HEADERS, 5.0)


def test_too_many_redirects_is_retryable_not_blocked(monkeypatch, fake_conns):
    _dns(monkeypatch, {"public.example.test": [(socket.AF_INET, PUBLIC_IP)]})
    fake_conns.handler = lambda conn: _Resp(302, "https://public.example.test/hop")
    with pytest.raises(RuntimeError, match="too many redirects"):
        post_guarded("https://public.example.test/hook", BODY, HEADERS, 5.0)


def test_cross_host_redirect_strips_signed_headers(monkeypatch, fake_conns):
    _dns(
        monkeypatch,
        {
            "a.example.test": [(socket.AF_INET, PUBLIC_IP)],
            "b.example.test": [(socket.AF_INET, PUBLIC_IP)],
        },
    )
    seen = []

    def handler(conn):
        seen.append((conn.real_host, dict(conn.request_headers)))
        if conn.real_host == "a.example.test":
            return _Resp(302, "https://b.example.test/other")
        return _Resp(200)

    fake_conns.handler = handler
    assert post_guarded("https://a.example.test/hook", BODY, HEADERS, 5.0) == 200
    assert [h for h, _ in seen] == ["a.example.test", "b.example.test"]
    first, second = seen[0][1], seen[1][1]
    assert first["X-Cadence-Signature"] == "sig"
    assert first["Authorization"] == "Bearer secret"
    assert "X-Cadence-Signature" not in second
    assert "X-Cadence-Event" not in second
    assert "Authorization" not in second
    assert second["Content-Type"] == "application/json"


def test_same_host_redirect_keeps_headers(monkeypatch, fake_conns):
    _dns(monkeypatch, {"a.example.test": [(socket.AF_INET, PUBLIC_IP)]})
    seen = []

    def handler(conn):
        seen.append(dict(conn.request_headers))
        if len(seen) == 1:
            return _Resp(302, "https://a.example.test/other")
        return _Resp(200)

    fake_conns.handler = handler
    assert post_guarded("https://a.example.test/hook", BODY, HEADERS, 5.0) == 200
    assert seen[1]["X-Cadence-Signature"] == "sig"


# --- pinning -----------------------------------------------------------------


def test_public_url_dials_validated_ip_with_hostname_headers(monkeypatch, fake_conns):
    calls: list = []
    _dns(monkeypatch, {"hook.example.com": [(socket.AF_INET, PUBLIC_IP)]}, calls)
    fake_conns.handler = lambda conn: _Resp(200)
    assert post_guarded("https://hook.example.com/hook", BODY, HEADERS, 5.0) == 200
    assert calls == ["hook.example.com"]
    conn = fake_conns.instances[0]
    assert conn.host_arg == PUBLIC_IP
    assert conn.real_host == "hook.example.com"
    assert conn.request_headers["Host"] == "hook.example.com"
    assert conn.method == "POST"


def test_rebinding_single_resolution_used_for_connection(monkeypatch, fake_conns):
    """A DNS record flipping public then private must not steer the request:
    the guard resolves once and dials the validated answer."""
    calls: list = []
    answers = [[(socket.AF_INET, PUBLIC_IP)], [(socket.AF_INET, "10.9.9.9")]]

    def fake(host, port, type=None, *args, **kwargs):
        calls.append(host)
        return _v4(answers[min(len(calls) - 1, 1)][0][1], port)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake)
    fake_conns.handler = lambda conn: _Resp(200)
    assert post_guarded("https://flaky.example.com/hook", BODY, HEADERS, 5.0) == 200
    assert calls == ["flaky.example.com"]
    assert fake_conns.instances[0].host_arg == PUBLIC_IP


def test_real_https_connection_uses_hostname_for_sni(monkeypatch):
    dialed = {}
    wrapped = {}

    def fake_create(addr, timeout=None, source_address=None):
        dialed.update(addr=addr)
        return object()

    class _Ctx:
        def wrap_socket(self, sock, server_hostname=None):
            wrapped.update(server_hostname=server_hostname)
            return sock

    monkeypatch.setattr(ssrf.socket, "create_connection", fake_create)
    conn = ssrf._PinnedHTTPSConnection(
        PUBLIC_IP, 443, timeout=5.0, real_host="hook.example.com", context=_Ctx()
    )
    conn.connect()
    assert dialed["addr"] == (PUBLIC_IP, 443)
    assert wrapped["server_hostname"] == "hook.example.com"


def test_ipv6_public_literal_with_explicit_port(monkeypatch, fake_conns):
    _no_dns(monkeypatch)
    fake_conns.handler = lambda conn: _Resp(200)
    url = f"https://[{PUBLIC_V6}]:8443/hook"
    assert post_guarded(url, BODY, HEADERS, 5.0) == 200
    conn = fake_conns.instances[0]
    assert conn.host_arg == PUBLIC_V6
    assert conn.port_arg == 8443
    assert conn.request_headers["Host"] == f"[{PUBLIC_V6}]:8443"


def test_allow_private_opt_out_passes_with_warning(monkeypatch, fake_conns, caplog):
    fake_conns.handler = lambda conn: _Resp(200)
    with caplog.at_level(logging.WARNING, logger="cadence.webhooks"):
        status = post_guarded("http://127.0.0.1/hook", BODY, HEADERS, 5.0, allow_private=True)
    assert status == 200
    assert "bypassed" in caplog.text


# --- dispatcher wiring --------------------------------------------------------


def _pending(db, hook):
    from datetime import datetime, timezone

    enqueue_event(
        db, "job.succeeded", {"k": "v"}, occurred_at=datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    )
    db.flush()
    from app.models.models import WebhookDelivery

    return db.query(WebhookDelivery).filter_by(webhook_id=hook.id).one()


def test_delivery_blocked_ssrf_is_terminal_without_retry(db_session, monkeypatch):
    from datetime import datetime, timezone

    # Prod default (guard on); conftest enables the opt-out for the suite
    # because the T0 test serves a real loopback receiver.
    monkeypatch.setattr(settings, "webhook_allow_private_ips", False)
    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    hook = webhook_row(db_session, url="http://127.0.0.1/hook")
    delivery = _pending(db_session, hook)
    assert dispatch_pending_deliveries(now=now, db=db_session) == 0
    db_session.refresh(delivery)
    assert delivery.status == "failed"
    assert delivery.attempt_count == 1
    assert delivery.completed_at == now
    assert delivery.last_error.startswith("blocked_ssrf:")
    assert dispatch_pending_deliveries(now=now, db=db_session) == 0


def test_delivery_respects_allow_private_setting(db_session, monkeypatch, fake_conns):
    from datetime import datetime, timezone

    now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(settings, "webhook_allow_private_ips", True)
    fake_conns.handler = lambda conn: _Resp(200)
    hook = webhook_row(db_session, url="http://127.0.0.1/hook")
    delivery = _pending(db_session, hook)
    assert dispatch_pending_deliveries(now=now, db=db_session) == 1
    db_session.refresh(delivery)
    assert delivery.status == "delivered"
