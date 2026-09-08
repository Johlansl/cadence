# Security

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- GitHub: use **Report a vulnerability** under the repository's *Security* tab
  (private vulnerability reporting), or
- email the maintainers at `<security contact>`.

Please include a description, affected version/commit, and a reproduction if you
have one. We aim to acknowledge within a few days.

## Threat model and known limitations

Cadence V1 is a **single-operator tool for a trusted network**. Read this before
exposing it beyond a LAN you control.

### One shared credential guards the dashboard and read/admin API

There is **no multi-user auth and no RBAC** (a deliberate V1 choice). Instead,
Caddy applies HTTP **basic auth**, a single shared username/password
(`CADENCE_DASHBOARD_*`), to everything except the agent endpoints
(`/api/v1/reports`, `/api/v1/agent/*`, `/api/v1/jobs/*/result`, which use
per-host signed-request tokens). It is **on by default**; `gen-secrets.sh`
generates the credential and Caddy binds to `127.0.0.1` unless you set
`CADENCE_HTTP_BIND=0.0.0.0`.

What this does *not* give you: per-user identity (writes are audited, but only
as far as the optional `X-Actor` header, see *Admin key* below), brute-force
protection at the proxy (rate-limit upstream if exposed), or defence against a
leaked shared password. `CADENCE_DASHBOARD_AUTH=off` removes the gate entirely,
only do that behind a VPN or on a management VLAN.

Behind that gate, every `GET` under `/api/v1` still returns the full fleet
picture, host list, per-host package inventory with exact installed/candidate
versions, job logs, maintenance windows, the fleet summary. Treat the dashboard
credential as protecting a "what is unpatched and where, and when it reboots"
map of your fleet.

Caddy sends a strict `Content-Security-Policy` (`default-src 'self'`, no
`unsafe-inline`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
`X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and
`Cross-Origin-Opener-Policy: same-origin`, and strips `Server`. There is no
HSTS: the default deployment uses an internal CA on a `.lan` hostname, where
HSTS would lock out a legitimate first visit. Add it (and drop `tls internal`)
if you move to a public domain with ACME.

### The server is fully trusted by every agent

The agent runs jobs the server hands it (`apt-get dist-upgrade`, `reboot`),
as root, with no client-side allow-list or confirmation. A compromised or
malicious server can upgrade and reboot the entire fleet. This is inherent to
the outbound-only, piggyback design.

### Admin key

A single shared `X-Admin-Key` secret authorizes every privileged action
(create/delete hosts, queue jobs and reboots on any host). There is no
per-purpose scoping, so a leak is fleet-wide, generate a strong value
(`scripts/gen-secrets.sh` or `openssl rand -hex 32`) and keep `.env` at mode
`0600`. The key can be rotated without downtime by setting the new value and
moving the old one to `CADENCE_ADMIN_KEY_PREVIOUS` while clients catch up.

Every successful admin write is recorded in the `audit_log` table (readable
at `GET /api/v1/admin/audit`, same key). Because the key is shared it cannot
prove *who* acted: the `actor` column is whatever the caller sent in an
optional `X-Actor` header, defaulting to `admin`. The `client` column is the
caller's IP; `X-Forwarded-For` is only honoured for connections from
`CADENCE_TRUSTED_PROXIES`, otherwise the direct peer is recorded. Rows are
pruned after `CADENCE_AUDIT_RETENTION_DAYS` (default 365).

### Agent tokens

Per-host tokens in the `agent_tokens` table (one per host at provisioning,
more can be issued). Each has an optional `expires_at` (defaults to
`CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS`, 365, unless the caller passes an
explicit one) and can be revoked at any time (`revoked_at`); a token is
accepted only while it is neither expired nor revoked *and* its host is
`is_active`. Issue, list (with a derived active/expired/revoked state) and
revoke via `/api/v1/admin/hosts/{id}/tokens`.

Agent requests are signed (agent `0.8.0`+: `X-Cadence-Token-Hash` /
`-Timestamp` / `-Signature`, an HMAC-SHA256 over the request keyed with the
token, verified against `hmac.compare_digest`); the raw token never crosses
the wire per call. A partial set of the signed headers is rejected outright.
Verifying a signature needs the real secret, so it is kept Fernet-encrypted
at rest (`CADENCE_TOKEN_ENCRYPTION_KEY`, same handling as `CADENCE_ADMIN_KEY`:
a plaintext credential in `.env`, mode 0600, backed up with it, rotatable
live via `_PREVIOUS`) alongside the one-way `token_hash` lookup key. A token
issued before this scheme existed has no encrypted copy and cannot
authenticate at all until its host is rotated onto a fresh token; see
`docs/decisions.md` "Authentication". The earlier `Authorization: Bearer
<token>` form is no longer accepted.

Rotation is roll-forward: issue a new token, move the agent onto it, then
revoke the old one, no window where the host cannot report. Revocation is
auth-plane only: it does **not** cancel a job already queued or running for
that host (deactivate the host to stop new jobs being handed out).

Caveats: a leaked token is valid until it expires or is revoked; the
plaintext is shown only once at issue time. A database
compromise now also exposes the encrypted copy for every token issued under
the signed scheme, recoverable by anyone who also holds
`CADENCE_TOKEN_ENCRYPTION_KEY` -- the same trade `CADENCE_ADMIN_KEY` already
makes, and no worse than the token itself already being usable by whoever
compromises the server (it needs the real secret to sign a verification-
equivalent too). A signed request's timestamp window
(`CADENCE_SIGNATURE_WINDOW_SECONDS`, 300s) bounds clock-skew tolerance but is
not full replay protection: a request captured inside that window could be
resent once, verbatim, before it expires. No nonce/replay tracking yet.

### Agent bootstrap is trust-on-first-use over plain HTTP

`scripts/agent-install.sh` (the `curl … | sudo sh` one-liner) fetches the agent
binary, its SHA-256 checksum, **and** the internal CA certificate over the same
**unauthenticated HTTP** channel (they have to be reachable before the host
trusts the CA). The checksum only guards against transport corruption, not
tampering: an attacker who can MITM that request can replace all three. This is
acceptable on a trusted LAN, the documented target, and risky anywhere else.
For a hostile network, transfer the CA and binary out of band and verify a
fingerprint you obtained separately.

The binary is tamper-evident: `scripts/publish-agent.sh` minisign-signs every
release (see [README](README.md#signed-agent-releases)). Pass the public key,
`agent/minisign.pub`, distributed **out of band**, not over the install channel,
to the installer as `CADENCE_MINISIGN_PUB`, and a bad or missing signature
aborts the install. Without it the installer uses the SHA-256 check only (the
LAN-target default). The CA certificate is still TOFU either way.

The pre-built binaries, the `.deb` packages and the container images attached to
each GitHub Release (`agent-v*` / `v*` tags) are a separate channel and are
**not** minisign- or GPG-signed, no long-lived signing key touches CI (see
[docs/decisions.md](docs/decisions.md#release-automation)). They instead carry a
Sigstore build-provenance attestation: `gh attestation verify <file-or-oci-ref>
--repo Johlansl/cadence`. The `.deb` is for a local `apt install ./…deb`, not an
apt repository.

### TLS uses an internal CA

Caddy issues certificates from its own CA. That CA root must be installed in
the system trust store of every monitored host and dashboard client. The CA
private key lives in the `caddy_data` volume, **back it up**; losing it breaks
TLS for every agent until they are re-provisioned. A public-domain / Let's
Encrypt setup requires editing the `Caddyfile`.

### The agent runs as root

Required for `apt-get` / `dpkg`. The `systemd` units apply light sandboxing;
they do not (and mostly cannot, given apt writes to `/usr`, `/boot`, `/etc`)
use the stricter `ProtectSystem` / capability-bounding directives.

### `POST /api/v1/reports` payload limits

The report body is stored verbatim in `reports.raw_payload` on every cycle, so
it is capped: a `Content-Length` over `CADENCE_MAX_REPORT_BYTES` (default 5 MiB)
is rejected with 413 before the body is read, and more than
`CADENCE_MAX_REPORT_PACKAGES` (default 10000) entries is a 422. Set either to
`0` to disable that check. A chunked request with no `Content-Length` slips past
the byte check but still hits the package cap; a reverse proxy (Caddy) can
enforce an absolute request-body ceiling.

### Advisory / CVE linkage is best-effort, not a vulnerability scan

The DSA/DLA and CVE ids shown next to a security update are enrichment of apt's
own security flag: the scheduler periodically fetches Debian's public advisory
lists and links an update only when an advisory's fixed version exactly matches
apt's candidate. It does not enumerate unfixed or no-DSA CVEs, carries no
severity, and can lag or miss while the feed is stale or a package's
binary→source name mapping is unknown. Absence of a linked advisory is not
evidence that a host is unaffected.
