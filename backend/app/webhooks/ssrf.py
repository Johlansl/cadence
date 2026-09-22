"""SSRF guard for outbound webhook deliveries (stdlib only).

Every webhook URL (initial or redirect target) goes through one DNS
resolution whose results are all checked against a deny-list of
non-public IP ranges. The HTTP connection then dials the validated IP
directly while keeping the original hostname for the Host header, TLS
SNI and certificate verification. A single resolution per hop means a
DNS record that flips between the check and the connection (DNS
rebinding) cannot steer the request: the connected IP is always one of
the validated answers.

Redirects are followed up to _MAX_REDIRECTS, each hop revalidated the
same way, and the request is re-POSTed with the same body. Sensitive
headers (Authorization and X-Cadence-*) are dropped when the redirect
leaves the current origin (scheme, host, port).
"""

from __future__ import annotations

import http.client
import ipaddress
import logging
import socket
import ssl
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("cadence.webhooks")

_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_BODY_READ_BYTES = 4096


class SSRFBlocked(Exception):
    """Raised when a webhook URL or redirect target is refused."""


def _ip_block_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return f"loopback address {ip}"
    if ip.is_link_local:
        return f"link-local address {ip}"
    if ip.is_multicast:
        return f"multicast address {ip}"
    if ip.is_unspecified:
        return f"unspecified address {ip}"
    if ip.is_reserved:
        return f"reserved address {ip}"
    if ip.is_private:
        return f"private address {ip}"
    if not ip.is_global:
        return f"non-public address {ip}"
    return None


def _check_ips(hostname: str, ip_strs: list[str], *, allow_private: bool) -> list[str]:
    """Validate every resolved IP. Returns the addresses to dial.

    Fails closed: one blocked address blocks the whole hostname. With
    allow_private, the check is skipped but the resolution result is
    still returned so the connection uses a single lookup.
    """
    if allow_private:
        blocked = []
        for raw in ip_strs:
            try:
                reason = _ip_block_reason(ipaddress.ip_address(raw))
            except ValueError:
                reason = f"unparsable address {raw!r}"
            if reason is not None:
                blocked.append(f"{raw} ({reason})")
        if blocked:
            log.warning(
                "webhook SSRF guard bypassed by operator opt-out",
                extra={"fields": {"host": hostname, "bypassed": "; ".join(blocked)}},
            )
        return ip_strs
    for raw in ip_strs:
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SSRFBlocked(f"unparsable address {raw!r} for host {hostname!r}") from exc
        reason = _ip_block_reason(ip)
        if reason is not None:
            raise SSRFBlocked(f"{reason} for host {hostname!r}")
    return ip_strs


def _resolve_validated(hostname: str, port: int, *, allow_private: bool) -> list[str]:
    """One DNS lookup for hostname, all answers validated. No second lookup
    happens later: callers must dial one of the returned addresses."""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        return _check_ips(hostname, [str(ip)], allow_private=allow_private)
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise
    ip_strs: list[str] = []
    for _family, _, _, _, sockaddr in answers:
        addr = sockaddr[0]
        if addr not in ip_strs:
            ip_strs.append(addr)
    if not ip_strs:
        raise SSRFBlocked(f"no addresses for host {hostname!r}")
    return _check_ips(hostname, ip_strs, allow_private=allow_private)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection dialing a validated IP. Host header carries the
    original hostname (set per request by the caller)."""

    def __init__(self, pinned_ip: str, port: int | None, timeout: float, real_host: str):
        super().__init__(pinned_ip, port, timeout=timeout)
        self._real_host = real_host


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection dialing a validated IP with SNI and certificate
    verification on the original hostname."""

    def __init__(
        self,
        pinned_ip: str,
        port: int | None,
        timeout: float,
        real_host: str,
        context: ssl.SSLContext,
    ):
        super().__init__(pinned_ip, port, timeout=timeout, context=context)
        self._real_host = real_host

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout, self.source_address)
        if getattr(self, "_tunnel_host", None):
            super().connect()
            sock.close()
            return
        self.sock = self._context.wrap_socket(sock, server_hostname=self._real_host)


_HTTP_CONN_CLS = _PinnedHTTPConnection
_HTTPS_CONN_CLS = _PinnedHTTPSConnection


def _host_header(hostname: str, port: int, scheme: str) -> str:
    default = 443 if scheme == "https" else 80
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if port == default:
        return hostname
    return f"{hostname}:{port}"


def _strip_on_origin_change(
    headers: dict[str, str], old_origin: tuple[str, str, int], new_origin: tuple[str, str, int]
) -> dict[str, str]:
    if old_origin == new_origin:
        return dict(headers)
    kept: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == "authorization" or lowered.startswith("x-cadence-"):
            continue
        kept[key] = value
    return kept


def _request_once(
    scheme: str,
    hostname: str,
    port: int,
    path: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
    *,
    allow_private: bool,
) -> tuple[int, str | None]:
    """POST once to a validated address. Returns (status, Location header)."""
    addrs = _resolve_validated(hostname, port, allow_private=allow_private)
    out_headers = dict(headers)
    out_headers["Host"] = _host_header(hostname, port, scheme)
    last_exc: Exception | None = None
    for addr in addrs:
        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                conn = _HTTPS_CONN_CLS(addr, port, timeout=timeout, real_host=hostname, context=ctx)
            else:
                conn = _HTTP_CONN_CLS(addr, port, timeout=timeout, real_host=hostname)
            conn.request("POST", path, body=body, headers=out_headers)
            resp = conn.getresponse()
            status = int(resp.status)
            location = resp.getheader("Location")
            try:
                resp.read(_BODY_READ_BYTES)
            finally:
                conn.close()
            return status, location
        except SSRFBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 -- try next validated address
            last_exc = exc
            continue
    assert last_exc is not None
    raise last_exc


def post_guarded(
    url: str, body: bytes, headers: dict[str, str], timeout: float, *, allow_private: bool = False
) -> int:
    """POST with SSRF deny-list, pinned connections and revalidated redirects.

    Raises SSRFBlocked for a refused URL (no retry: deterministic), other
    exceptions for transport failures (retryable by the dispatcher).
    """
    current_url = url
    current_headers = dict(headers)
    for _ in range(_MAX_REDIRECTS + 1):
        parts = urlsplit(current_url)
        if parts.scheme not in ("http", "https"):
            raise SSRFBlocked(f"refused scheme {parts.scheme!r} for {current_url!r}")
        if parts.username is not None or parts.password is not None:
            raise SSRFBlocked(f"userinfo in URL is refused for host {parts.hostname!r}")
        hostname = parts.hostname
        if not hostname:
            raise SSRFBlocked(f"no host in URL {current_url!r}")
        try:
            port = parts.port
        except ValueError as exc:
            raise SSRFBlocked(f"invalid port in URL {current_url!r}") from exc
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if not 1 <= port <= 65535:
            raise SSRFBlocked(f"invalid port {port} in URL {current_url!r}")
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        status, location = _request_once(
            parts.scheme, hostname, port, path, body, current_headers, timeout,
            allow_private=allow_private,
        )
        if status not in _REDIRECT_STATUSES or not location:
            return status
        next_url = urljoin(current_url, location)
        next_parts = urlsplit(next_url)
        old_origin = (parts.scheme, hostname.lower(), port)
        try:
            next_port = next_parts.port
        except ValueError as exc:
            raise SSRFBlocked(f"invalid port in redirect to {next_url!r}") from exc
        if next_port is None:
            next_port = 443 if next_parts.scheme == "https" else 80
        new_origin = (next_parts.scheme, (next_parts.hostname or "").lower(), next_port)
        current_headers = _strip_on_origin_change(current_headers, old_origin, new_origin)
        current_url = next_url
    raise RuntimeError(f"too many redirects (max {_MAX_REDIRECTS}) starting at {url!r}")
