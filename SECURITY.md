# Security

## Reporting a vulnerability

Please report security issues **privately**, not as a public issue:

- GitHub: open a draft advisory at
  `https://github.com/Johlansl/cadence/security/advisories/new`
  (**Report a vulnerability** under the repository's *Security* tab,
  private vulnerability reporting).

Please include a description, affected version/commit, and a reproduction if you
have one. We aim to acknowledge within a few days.

## Threat model and known limitations

Cadence V1 is a **single-operator tool for a trusted network**. Read this before
exposing it beyond a LAN you control.

### One shared credential guards the dashboard and read/admin API

There is no user management, and RBAC is two roles only (a deliberate
V1 choice): OIDC signs operators in individually; an account listed in
`CADENCE_OIDC_OPERATOR_EMAILS` is an **operator** with full powers, every
other signed-in account is a **reader** (reads only, writes refused with
403). The shared `X-Admin-Key` bypasses roles entirely. Instead,
Caddy applies HTTP **basic auth**, a single shared username/password
(`CADENCE_DASHBOARD_*`), to everything except the agent endpoints
(`/api/v1/reports`, `/api/v1/agent/*`, `/api/v1/jobs/*/result`, which use
per-host signed-request tokens). It is **on by default**; `gen-secrets.sh`
generates the credential and Caddy binds to `127.0.0.1` unless you set
`CADENCE_HTTP_BIND=0.0.0.0`.

The interactive API docs (`/docs`, `/redoc`) and the OpenAPI schema
(`/openapi.json`) are **disabled in production by default** and gated behind
`CADENCE_API_DOCS_ENABLED` (set it true only in a dev `.env`). When enabled
they are served through Caddy behind the same basic-auth as the read API.

What this does *not* give you: per-user identity on the shared-key path
(key writes are audited only as far as the optional `X-Actor` header, see
*Admin key* below; OIDC-session writes carry the verified subject instead,
see *Admin SSO*), brute-force
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

### Admin SSO (OIDC)

Operators can additionally sign in through an OIDC provider (see
`docs/oidc.md`; inert unless `CADENCE_OIDC_ENABLED=true` with issuer, client
id/secret and redirect URI all set). A signed-in operator's writes record the
verified subject as `actor`, and a self-asserted `X-Actor` is ignored on a
session. New trust this adds, stated plainly:

- a compromised provider (or its signing keys) mints admin sessions until
  they expire (default 8 h); the shared key path is unaffected and stays
  available as break-glass;
- a stolen session cookie impersonates that operator until expiry: cookies
  are HttpOnly/Secure/SameSite=Lax, served over the Caddy TLS front only,
  and there is no per-session revoke -- emergency revocation is a
  `CADENCE_TOKEN_ENCRYPTION_KEY` rotation (kills every session and every
  agent token at once, same story as the token plane);
- logout ends the Cadence session only, not the provider SSO session;
- sign-in transport failures (dead provider) answer 502/401 with no detail
  to the browser and no traceback in the log;
- operator membership is static config (`CADENCE_OIDC_OPERATOR_EMAILS`,
  matched against the session actor or token email); an account not
  listed is a reader, and sessions sealed before RBAC read as reader.
  Authenticated-but-forbidden writes answer 403; the frontend hides
  nothing (write buttons refuse with an explanation, the backend is the
  boundary);
- historic `admin` audit rows are never rewritten and keep meaning
  "someone with the shared key", before or after SSO exists.

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

### Agent bootstrap pins trust before executing downloaded code

An enrollment code is manually transferred to the target host. Its single
string contains both a 192-bit authentication secret and the SHA-256
fingerprint of the exact Caddy server-CA file. It expires after 30 minutes by
default and the backend consumes it atomically on the first successful use.

`scripts/agent-bootstrap.sh` must itself arrive through an authenticated
channel, such as `scp` from the server checkout or the authenticated dashboard.
It downloads only `/agent/ca.crt` over unauthenticated HTTP, verifies its exact
bytes against the fingerprint in the code, and aborts on a mismatch. The
installer, agent binary, checksum, optional minisign signature and systemd
units are fetched only afterwards, over HTTPS rooted in that verified CA. The
enrollment secret is therefore never sent before the server has been
authenticated, and no script obtained over HTTP gets to decide whether the
fingerprint check runs.

The agent generates its ECDSA P-256 private key locally and sends only a CSR.
The returned 90-day certificate authenticates the TLS transport on the
dedicated agent port; the existing per-request HMAC remains required as an
independent application-authentication layer. Minisign remains available as an
additional release-signing check, not as the root of first-contact trust.

The pre-built binaries, the `.deb` packages and the container images attached to
each GitHub Release (`agent-v*` / `v*` tags) are a separate channel and are
**not** minisign- or GPG-signed, no long-lived signing key touches CI (see
[docs/decisions.md](docs/decisions.md#release-automation)). They instead carry a
Sigstore build-provenance attestation: `gh attestation verify <file-or-oci-ref>
--repo Johlansl/cadence`. The `.deb` is for a local `apt install ./…deb`, not an
apt repository.

### TLS uses an internal CA

Caddy issues certificates from its own CA. That CA root must be installed in
the system trust store of every monitored host and dashboard client. Its
private key lives in `caddy_data`. A separate client root and intermediate in
the backend-only `client_pki` volume issue agent certificates trusted on port
8443. **Back up both volumes**; losing either PKI requires fleet
re-enrollment. A public-domain / Let's Encrypt server setup does not replace
the private client PKI.

### The agent runs as root

Required for `apt-get` / `dpkg`. The `systemd` units apply light sandboxing;
they do not (and mostly cannot, given apt writes to `/usr`, `/boot`, `/etc`)
use the stricter `ProtectSystem` / capability-bounding directives. What is kept
is `ProtectHome`, `PrivateTmp`, `ProtectControlGroups` and `LockPersonality`.

`RestrictSUIDSGID=true` used to be in that list and was removed in agent
`0.12.1`. It stopped the agent process from creating or changing setuid/setgid
files, which meant a routine upgrade of any package that ships one (`shadow`
installing `newgrp` / `chage` setuid root, `sudo`, `mount`, `ping`, ...) failed
under the agent unit while succeeding for the same `dpkg` run by hand. That
protection was aimed at a narrow case: the agent binary itself being exploited
*during* a `dpkg` run and using the elevated moment to drop a setuid backdoor.
The residual risk after removing it is exactly that case, and it is judged
acceptable for now because the agent already runs as root for the duration of
the upgrade (a compromised agent has many equivalent options, setuid or not),
the code is stdlib-only with no network input it acts on (communication is
outbound-only, the server never pushes), and the alternative, an upgrade tool
that breaks upgrades silently, is worse for the operator than the marginal
hardening was worth. The primary defence against a compromised *server* is
unchanged: it is the outbound-only model, not this directive. If a future
change narrows what the agent executes (for example a dedicated non-root helper
for the non-apt work), reinstating `RestrictSUIDSGID` on that part should be
reconsidered.

### `POST /api/v1/reports` payload limits

The report body is stored verbatim in `reports.raw_payload` on every cycle, so
it is capped: a `Content-Length` over `CADENCE_MAX_REPORT_BYTES` (default 5 MiB)
is rejected with 413 before the body is read, and more than
`CADENCE_MAX_REPORT_PACKAGES` (default 10000) entries is a 422. Set either to
`0` to disable that check. A chunked request with no `Content-Length` slips past
the byte check but still hits the package cap; a reverse proxy (Caddy) can
enforce an absolute request-body ceiling.

### Outbound webhooks are admin-configured and SSRF-guarded

Each webhook's signing secret is stored Fernet-encrypted at rest
(`webhooks.secret_encrypted`, `CADENCE_TOKEN_ENCRYPTION_KEY`, the same scheme as
agent tokens) and is returned in plaintext only once, in the creation response,
alongside the full URL; every later read masks the URL and omits the secret.
Each delivery is signed (`X-Cadence-Signature` = HMAC-SHA256 over the send
timestamp and the body hash, keyed with that secret) with the timestamp in the
signed scope, so a receiver that checks both the signature and a timestamp
freshness window rejects replays.

Three things a webhook operator should know. Deliveries go through an SSRF
guard (`backend/app/webhooks/ssrf.py`, stdlib only): every URL is resolved
once per hop and every returned IP is checked against a deny-list of
non-public ranges (loopback, private, link-local including the cloud metadata
address, multicast, reserved, unspecified). The connection then dials the
validated IP directly while Host, TLS SNI and certificate verification keep
using the original hostname, so a DNS record that changes between the check
and the connection cannot steer the request elsewhere. Up to 3 redirects are
followed and each hop is revalidated the same way; `Authorization` and
`X-Cadence-*` headers are dropped when a redirect leaves the current origin
(scheme, host, port), and userinfo in URLs plus non-http(s) schemes (including
`ftp` redirect targets) are always refused. A blocked delivery is parked
`failed` with `last_error` prefixed `blocked_ssrf:` and is not retried.

The guard is on by default. `CADENCE_WEBHOOK_ALLOW_PRIVATE_IPS=true` disables
the deny-list for operators who deliberately target a private or loopback
address on a trusted network; every bypass is logged as a warning. The remaining
trust boundary is unchanged: only a holder of `X-Admin-Key` can create or
change a webhook URL (the `GET` views are unauthenticated but expose neither
the raw URL nor the secret and cannot mutate anything), so the
outbound-request surface stays at the same trust level as
`CADENCE_ADVISORY_FEED_URLS`, an operator input.

### Advisory / CVE linkage is best-effort, not a vulnerability scan

The DSA/DLA and CVE ids shown next to a security update are enrichment of apt's
own security flag: the scheduler periodically fetches Debian's public advisory
lists and links an update only when an advisory's fixed version exactly matches
apt's candidate. It does not enumerate unfixed or no-DSA CVEs, and can lag or
miss while the feed is stale or a package's binary→source name mapping is
unknown. Absence of a linked advisory is not evidence that a host is
unaffected.

Each linked CVE may additionally show a cached CVSS base score and severity,
resolved from the NVD by the scheduler (see `docs/decisions.md`, "Updates").
That score is the same class of best-effort enrichment: a CVE with no known
score shows nothing extra, and the absence of a score is not evidence that a
host is unaffected either.
