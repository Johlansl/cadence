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

### The API's read endpoints are unauthenticated

Every `GET` under `/api/v1` (host list, per-host package inventory with exact
installed/candidate versions, job logs, maintenance windows, fleet summary) is
served without any credential. Anyone who can reach the site gets a full,
ranked "what is unpatched and where, and when it will be down for a reboot" map
of the fleet.

This is a deliberate V1 choice (no multi-user, no RBAC). The reverse proxy
(`Caddyfile`) ships with **no** auth in front of `/api`. If you deploy Cadence
anywhere reachable by untrusted parties, put authentication in front of it
(HTTP basic auth at Caddy, an allow-list, a VPN, …).

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
