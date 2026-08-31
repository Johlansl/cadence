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
Caddy applies HTTP **basic auth** — a single shared username/password
(`CADENCE_DASHBOARD_*`) — to everything except the agent endpoints
(`/api/v1/reports`, `/api/v1/agent/*`, `/api/v1/jobs/*/result`, which use
per-host Bearer tokens). It is **on by default**; `gen-secrets.sh` generates the
credential and Caddy binds to `127.0.0.1` unless you set
`CADENCE_HTTP_BIND=0.0.0.0`.

What this does *not* give you: per-user identity or audit, brute-force
protection at the proxy (rate-limit upstream if exposed), or defence against a
leaked shared password. `CADENCE_DASHBOARD_AUTH=off` removes the gate entirely —
only do that behind a VPN or on a management VLAN.

Behind that gate, every `GET` under `/api/v1` still returns the full fleet
picture — host list, per-host package inventory with exact installed/candidate
versions, job logs, maintenance windows, the fleet summary. Treat the dashboard
credential as protecting a "what is unpatched and where, and when it reboots"
map of your fleet.

### The server is fully trusted by every agent

The agent runs jobs the server hands it (`apt-get dist-upgrade`, `reboot`),
as root, with no client-side allow-list or confirmation. A compromised or
malicious server can upgrade and reboot the entire fleet. This is inherent to
the outbound-only, piggyback design.

### Admin key

A single shared `X-Admin-Key` secret authorizes every privileged action
(create/delete hosts, queue jobs and reboots on any host). There is no rotation
mechanism, no per-purpose scoping, and no audit log. Treat a leak as
fleet-wide. Generate a strong value (`scripts/gen-secrets.sh` or
`openssl rand -hex 32`) and keep `.env` at mode `0600`.

### Agent tokens

One bearer token per host, SHA-256-hashed at rest. There is **no rotation, no
expiry, and no revocation** short of deactivating (`is_active=false`) or
deleting the host. A leaked token is valid indefinitely and can be replayed
from anywhere.

### Agent bootstrap is trust-on-first-use over plain HTTP

`scripts/agent-install.sh` (the `curl … | sudo sh` one-liner) fetches the agent
binary, its SHA-256 checksum, **and** the internal CA certificate over the same
**unauthenticated HTTP** channel (they have to be reachable before the host
trusts the CA). The checksum only guards against transport corruption, not
tampering: an attacker who can MITM that request can replace all three. This is
acceptable on a trusted LAN — the documented target — and risky anywhere else.
For a hostile network, transfer the CA and binary out of band and verify a
fingerprint you obtained separately.

### TLS uses an internal CA

Caddy issues certificates from its own CA. That CA root must be installed in
the system trust store of every monitored host and dashboard client. The CA
private key lives in the `caddy_data` volume — **back it up**; losing it breaks
TLS for every agent until they are re-provisioned. A public-domain / Let's
Encrypt setup requires editing the `Caddyfile`.

### The agent runs as root

Required for `apt-get` / `dpkg`. The `systemd` units apply light sandboxing;
they do not (and mostly cannot, given apt writes to `/usr`, `/boot`, `/etc`)
use the stricter `ProtectSystem` / capability-bounding directives.

### `POST /api/v1/reports` has no payload size limit

An authenticated agent (or a leaked token) can push an arbitrarily large report
body, stored verbatim in `reports.raw_payload` on every cycle.
