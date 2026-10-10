# Cadence

**Lightweight patch management for Linux servers.** A small Go agent reports
what's installed and what updates are pending; a web dashboard shows the fleet
at a glance and lets you trigger `apt` upgrades (and reboots) per host, on
demand or on a schedule.

Cadence aims to sit between tools that are too heavy for a small fleet or a
homelab (RHEL Satellite / Uyuni: many GB of RAM, an imposed host OS, mandatory
DNS) and tools that only show you the problem without fixing it. It gives you
**visibility and remediation** in one place, on a 2 vCPU / 2 GB box.

> **Status: v1, single-operator.** It works and is in real use. A single shared
> basic-auth credential gates the dashboard and read/admin API (on by default),
> and agents fully trust the server. Operators can also sign in individually via
> OIDC, with two roles only: an account listed in `CADENCE_OIDC_OPERATOR_EMAILS`
> is an operator, every other signed-in account is a reader (reads only), and
> the shared admin key bypasses roles. There is no user management beyond that.
> Read [SECURITY.md](SECURITY.md) before exposing it beyond a trusted network.

## What it does

- **Inventory**: installed packages and available `apt` updates per host, with
  a best-effort security-vs-normal split.
- **Fleet view**: every host with a status badge, update counts, a last-seen
  freshness indicator (spots a stopped agent), tags and filters, and a fleet
  summary.
- **Remediation**: trigger `apt-get dist-upgrade` on a host from the dashboard;
  the agent runs it within ~1 minute and posts the log back. A "dry run"
  button previews what the upgrade would change (packages upgraded, installed,
  removed, kept back) without touching the host.
- **Upgrade health checks**: validate disk, dpkg, apt repositories and package
  locks before an upgrade, then verify dpkg, apt, failed services, disk and the
  reboot signal afterwards. The dashboard keeps apt's result separate from the
  resulting host health. A standalone `health_check` job re-runs the
  post-checks with no upgrade attached (manually, or once per boot), so health
  never goes stale between upgrades.
- **Package exclusions**: keep packages or families off automatic upgrades,
  globally, per host, or per tag (`linux-image*`, `docker-ce`, ...). The
  server resolves the rules per host and the agent reconciles dpkg holds
  toward them.
- **Security advisories**: pending security updates link to their Debian
  DSA/DLA advisories, and each linked CVE can show a cached CVSS base score
  and severity resolved from the NVD by the scheduler. Best-effort
  enrichment, not a vulnerability scan.
- **Reboots**: per-host policy (`never` / `auto` / `prompt`), overridable per
  job; the agent reboots only when the upgrade actually left one pending. In a
  campaign a rebooting host is not done until it proves its return (a new kernel
  boot id plus fresh health), a reboot the server cannot prove stops the
  campaign, and an optional fleet-wide budget (`CADENCE_MAX_CONCURRENT_REBOOTS`,
  off by default) caps the reboots still awaiting proof. See
  [docs/campaigns.md](docs/campaigns.md).
- **Maintenance windows**: one recurring weekly/monthly window per host,
  turned into jobs by a scheduler service.
- **Campaigns**: roll a dist-upgrade across many hosts in ordered waves, with
  a global concurrency cap, an observation pause between waves, and an
  automatic stop when failures pile up.
- **Agent upgrades**: an `agent_upgrade` job, on its own or in a campaign
  (canary, observation gate, stop on failure), replaces the agent binary on a
  host from your own Cadence server. The agent checks the signature against the
  trust set compiled into it, the checksum, the architecture and the version
  before it installs; the server calls the job a success only once the host
  reports the new version; and a target older than the running version is
  refused. Hosts need agent 0.15.0 or later, see
  [Upgrading the agent fleet](#upgrading-the-agent-fleet).
- **History & retention**: append-only report and job logs, pruned on a
  configurable schedule.

Not in v1 (see [docs/decisions.md](docs/decisions.md#v1-scope)):
non-apt package managers, and user management beyond the two OIDC roles
(operator and reader).
Webhooks carry one generic signed JSON shape; chat-native formatting
(Discord / Slack / Teams) stays out of scope by design, relay it yourself
(see [docs/webhooks.md](docs/webhooks.md)).

## How it fits together

```
                          monitored hosts
                     ┌───────────────────────┐
                     │  cadence-agent (Go)   │   systemd timers:
                     │  one-shot, static     │   ~30 min report, ~1 min job poll
                     └───────────┬───────────┘
                        outbound │ HTTPS only
                                 ▼
   ┌───────────────────── central server (docker compose) ──────────────────┐
   │  caddy ──▶ frontend (nginx + React)  ──▶  backend (FastAPI) ──▶  db     │
   │   TLS        dashboard + /api proxy         HTTP API           Postgres │
   │                                            ▲                            │
   │                                   scheduler │ (same image as backend)   │
   │                          due windows → jobs, reaper, retention          │
   └───────────────────────────────────────────────────────────────────────┘
```

The agent only ever makes outbound calls. A job triggered from the dashboard is
delivered on the response to the agent's next report (*piggyback*) or claimed
by the dedicated ~1 min poll. Details: [docs/architecture.md](docs/architecture.md).

## Screenshots

<!-- Add PNGs under docs/img/ and reference them here:
     ![Fleet overview](docs/img/fleet.png)
     ![Host detail](docs/img/host.png)
-->

## Quick start: central server

Requires Docker with the Compose plugin. On the server:

```sh
git clone <repo-url> cadence && cd cadence

scripts/gen-secrets.sh          # writes .env with random secrets; prints the dashboard login ONCE
$EDITOR .env                    # set CADENCE_SITE_ADDRESS; set CADENCE_HTTP_BIND=0.0.0.0 to serve the LAN

docker compose up -d --build    # first run must build; Alembic creates the schema on boot
```

Save the dashboard user/password `gen-secrets.sh` printed, only the bcrypt hash
is kept in `.env`. To change it later, run `scripts/rotate-dashboard-password.sh`
then `docker compose up -d caddy` (don't hand-edit the hash, every `$` in it
must be doubled for `docker compose`, and Caddy now refuses to start if it
isn't).

Check it:

```sh
curl -s http://127.0.0.1:8000/healthz              # {"status":"ok"}
docker compose run --rm backend alembic current    # <latest revision> (head)
```

Open the dashboard at `https://<CADENCE_SITE_ADDRESS>/` and log in with those
credentials. Caddy terminates TLS on 80/443 (80 redirects to 443) using its own
internal CA, and gates everything except the agent endpoints with basic auth.
By default Caddy binds to **`127.0.0.1`** (reachable only from the server), set
`CADENCE_HTTP_BIND=0.0.0.0` in `.env` to serve the LAN. The `backend` (8000) and
plain-HTTP `frontend` (8080) ports always stay on loopback. `restart:
unless-stopped` brings everything back after a reboot.

`CADENCE_SITE_ADDRESS` must resolve to the server from the server itself and
from every monitored host and browser (LAN DNS, or `/etc/hosts` entries). For a
public domain with Let's Encrypt instead of the internal CA, edit the
`Caddyfile` (drop `tls internal`, add an ACME email).

### Run from published images

Instead of `--build`, pull the images published to GHCR on each `v*` release
(backend + frontend; `db` and `caddy` already come from upstream). Add the
overlay and set `CADENCE_VERSION` in `.env` (omit it for `:latest`):

```sh
echo 'CADENCE_VERSION=<version>' >> .env   # a published v* release, see the Releases page
docker compose -f docker-compose.yml -f docker-compose.release.yml pull
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d
```

Each image carries a Sigstore build-provenance attestation:

```sh
gh attestation verify oci://ghcr.io/johlansl/cadence-backend:<version> --repo Johlansl/cadence
```

`scripts/deploy.sh` on the central server always builds from source and ignores
this overlay.

## Add a monitored host

**1. Publish an agent version, then stage the bootstrap assets** (once per
server; repeat the version publish for each agent release).

The server needs `minisign` (`apt-get install -y minisign` on Debian/Ubuntu)
and `curl`, and the publishing host must be linux/amd64: once the signature
verifies, `publish-agent.sh` runs the staged amd64 binary with `-version`, and
it refuses to publish from any other platform.

```sh
# normal path: download release agent-v<version> from GitHub, verify it against
# the two trusted keys (see "Signed agent releases"), stage it atomically
scripts/publish-agent.sh fetch <version>
scripts/publish-agent.sh assets                      # installer + units + CA
```

`fetch` trusts nothing it downloads: a version is staged only if its signatures
verify against the committed keys, whatever the download looked like. For an
air-gapped server, or for a release built by hand with the break-glass tool,
verify and stage a local directory instead:

```sh
scripts/publish-agent.sh publish <version> <dir>     # verify + atomic stage
```

This publishes the CA at the one intentionally unauthenticated path and every
executable artifact over HTTPS only. Published versions are immutable: a bad
release needs a new version, never a republish.

**2. Create an enrollment code** (on the server, reads `CADENCE_ADMIN_KEY`
from `.env`):

```sh
scripts/provision-host.sh vm-web-01 "web frontend"
```

The code expires after 30 minutes by default, is shown once, and is consumed by
its first successful use. It contains both a 192-bit secret and the SHA-256
fingerprint of the exact server CA file the new host must trust. To migrate an
existing host without replacing its database identity:

```sh
scripts/provision-host.sh --host-id <existing-host-uuid> vm-web-01
```

**3. Run the trusted bootstrap on the host.** Copy
`scripts/agent-bootstrap.sh` from the server checkout through an authenticated
channel such as `scp`; never fetch the bootstrap script itself over HTTP. Then:

```sh
sudo CADENCE_DASHBOARD_URL=https://<site> ./agent-bootstrap.sh
```

Enter the enrollment code only at its prompt. The prelude downloads
`/agent/ca.crt` over HTTP, hashes its exact bytes, and stops on any mismatch.
Only after a match does it download the installer, binary, checksum, optional
minisign signature and systemd units over HTTPS rooted in that CA. The agent
creates its private ECDSA key locally, receives a 90-day client certificate,
stores the HMAC token and versioned certificate files under `/etc/cadence`, and
switches post-enrollment traffic to `https://<site>:8443`. All three timers are
then enabled.

Verify:

```sh
sudo systemctl start cadence-agent.service
journalctl -u cadence-agent -n 20 --no-pager        # "report sent to ..."
curl -s https://<site>/api/v1/hosts                  # the host appears, last_seen_at fresh
```

### Install from a `.deb`

Each `agent-v*` release also ships a `.deb` (`amd64` + `arm64`) as a Release
asset, for hosts you manage with a configuration tool. It installs the binary
and `systemd` units but deliberately does not establish first-contact trust or
create credentials; fresh hosts must still complete enrollment first. The
`.deb` is not minisign-signed: check it with the `.sha256` file and the
attestation, as below.

```sh
VER=0.15.0; ARCH=amd64   # the release you want
curl -fsSLO "https://github.com/Johlansl/cadence/releases/download/agent-v$VER/cadence-agent_${VER}-1_${ARCH}.deb"
curl -fsSLO "https://github.com/Johlansl/cadence/releases/download/agent-v$VER/cadence-agent_${VER}-1_${ARCH}.deb.sha256"
sha256sum -c "cadence-agent_${VER}-1_${ARCH}.deb.sha256"
gh attestation verify "cadence-agent_${VER}-1_${ARCH}.deb" --repo Johlansl/cadence   # optional

sudo apt install "./cadence-agent_${VER}-1_${ARCH}.deb"
# Existing /etc/cadence/agent.env and mTLS credentials are preserved.
```

The `.deb` is not served from an apt repository, so `apt upgrade` never picks up
a newer agent: to move a host forward, download the new `.deb` from the Release
and install it the same way (or use an `agent_upgrade` campaign, see
[Upgrading the agent fleet](#upgrading-the-agent-fleet)).

### Trust Caddy's internal CA on a host

```sh
# on the server, stack up:
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > cadence-ca.crt
# on the host:
sudo cp cadence-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

The fingerprint-pinned bootstrap does this for you.

### Signed agent releases

Every agent release is signed with
[minisign](https://jedisct1.github.io/minisign/), and the server stages a
version only if its signature verifies. The trust set is two public keys that
are committed in the repository and compiled into the agent:

| Key | File | Key ID | Role |
|---|---|---|---|
| Active | `agent/minisign.pub` | `C9EDE89E1DEEED37` | signs every release, in CI |
| Backup | `agent/minisign-backup.pub` | `2167C9939EC0F4FC` | kept offline by the maintainer; signs only an emergency release if the active key is lost or compromised |

A signature is accepted when either key verifies it (active key first). The two
must be different keys: `publish-agent.sh` and the agent both refuse a trust set
that collapses to one.

**How a release is made.** Pushing an `agent-v<version>` tag on the repository
that runs `.github/workflows/release.yml` starts three jobs. `build` produces
the binaries and `.deb` packages and attests them with Sigstore. `sign` runs in
the GitHub environment `release`, the only job that sees the secret
`MINISIGN_SECRET_KEY`: it signs both binaries and checks every signature against
`agent/minisign.pub` before it goes on. `publish` creates the GitHub Release as
a **draft**. The maintainer verifies the `.minisig` files by hand and then
publishes the draft; nothing is published automatically. The protection of the
`release` environment (required reviewer, restriction to `agent-v*` tags) is a
repository setting that the workflow file cannot enforce, see
[SECURITY.md](SECURITY.md#release-signing).

**The server only verifies.** `publish-agent.sh fetch` downloads six assets
(for amd64 and arm64: the binary, its `.sha256` and its `.minisig`) from the
published release and stages them after checking the signatures against the
trust set above. The server never holds a private key and never builds release
bytes. This matters: had the server built what gets signed, a root-compromised
server could obtain a signature on an arbitrary binary just by serving malicious
build output. Unsigned publishing does not exist, and `publish-agent.sh` refuses
to run without both public keys.

**Break-glass.** If the active key is lost or compromised, or GitHub cannot be
used, `scripts/build-agent-release.sh` builds, signs and self-checks a release
by hand with the key named by `CADENCE_SIGNING_KEY` (normally the offline backup
key; there is no default key path). Its signatures are verified against both
public keys and the log names the one that matched. The result goes to the
server with `publish-agent.sh publish <version> <dir>`, or, for servers that use
`fetch`, the six files are uploaded by hand to a GitHub Release of the same tag.
The header of the script
and [docs/decisions.md](docs/decisions.md#agent-distribution--signing) describe it;
the full procedures after a key compromise are in
[SECURITY.md](SECURITY.md#if-a-signing-key-is-compromised).

**An optional check on every host.** Install `minisign` on the host first
(`apt-get install -y minisign` on Debian/Ubuntu), copy the public keys to it out
of band, and name them when you run the trusted bootstrap:

```sh
sudo CADENCE_DASHBOARD_URL=https://cadence.lan \
  CADENCE_MINISIGN_PUB=/root/minisign.pub \
  CADENCE_MINISIGN_PUB2=/root/minisign-backup.pub \
  ./agent-bootstrap.sh
```

- Each value is either the path of a key file (`agent/minisign.pub` as it is in
  the repository: exactly two lines, the `untrusted comment:` line and the key
  line, with LF line endings) or a bare key (`RW` followed by 54 base64
  characters, for example `"$(tail -n1 agent/minisign.pub)"`). Pasting the whole
  content of a key file as the value is refused.
- `CADENCE_MINISIGN_PUB2` is optional. The installer tries `CADENCE_MINISIGN_PUB`
  first, then `CADENCE_MINISIGN_PUB2` only if the first did not verify, and accepts
  the binary if either does. The log names the key that verified, and a failure
  lists the key IDs it tried. `CADENCE_MINISIGN_PUB2` without
  `CADENCE_MINISIGN_PUB` is an error.
- Both values are checked for shape before anything is downloaded, even when the
  first key would verify: a malformed second key stops the install. Keys are never
  fetched from the network.
- If `minisign` is not installed the installer stops and tells you to install it.
  Without `CADENCE_MINISIGN_PUB` only the SHA-256 published next to the binary is
  checked, and that comes from the same server (see
  [SECURITY.md](SECURITY.md#release-signing)).

Minisign is an extra release-authenticity layer; server authentication already
comes from the CA fingerprint carried in the enrollment code.

**The offline backup key.** It is generated offline, never on a Cadence machine,
and kept by the maintainer. `scripts/backup-signing-key.sh <key file>` writes an
encrypted copy next to it after checking that it is one of the two trusted keys.
`scripts/restore-signing-key.sh --to <path> <backup>` restores it, still
encrypted, to a location you name (not inside a git repository; use `/dev/shm`
and delete the file after use). Server backups deliberately never include key
material.

**The GitHub Release channel.** The binaries attached to each GitHub Release
(`agent-v*` tag) carry their `.minisig` (signed by the active key) and a Sigstore
build-provenance attestation. The `.deb` packages and the container images carry
the attestation only.

```sh
gh release download agent-v<version> --repo Johlansl/cadence -p 'cadence-agent-linux-amd64*'
sha256sum -c cadence-agent-linux-amd64.sha256
minisign -V -p agent/minisign.pub -m cadence-agent-linux-amd64 \
  -x cadence-agent-linux-amd64.minisig
gh attestation verify cadence-agent-linux-amd64 --repo Johlansl/cadence
```

Releases from `0.15.0` on have a `.minisig`; earlier ones only have the
attestation.

**Running your own Cadence (a fork)?** The committed keys are the upstream
project's, and you cannot hold their private halves, so `publish-agent.sh` would
reject anything you build. Generate two key pairs of your own, offline:

```sh
# -f replaces the committed upstream .pub files
minisign -G -W -f -p agent/minisign.pub        -s /path/to/active.key  # CI signing key, no password (CI cannot answer a prompt)
minisign -G    -f -p agent/minisign-backup.pub -s /path/to/backup.key  # offline backup key, with a password
```

Then update `activePubKeyText` and `backupPubKeyText` in
`agent/internal/upgrade/trust.go` to the same two keys (a test fails if they
drift from the `.pub` files). To sign in CI, create a GitHub environment named
`release` on your repository with the active key's secret file as
`MINISIGN_SECRET_KEY` (then delete that file: the active key should live only in
the secret); to download your releases instead of upstream's, set
`CADENCE_FETCH_REPO=<owner>/<repo>` for `publish-agent.sh fetch`. There is no
unsigned mode. See [docs/decisions.md](docs/decisions.md#agent-distribution--signing).

## Configuration

All configuration is environment variables. Server variables live in `.env`
(read by `docker compose`); agent variables live in `/etc/cadence/agent.env`.

### Server (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_DB` | `cadence` / `cadence` | database role and name |
| `POSTGRES_PASSWORD` | - | **read only on first boot** of the `pgdata` volume; changing it later needs `down -v` or an `ALTER ROLE` |
| `CADENCE_ADMIN_KEY` | - | shared secret for every admin write (`X-Admin-Key`). Use a strong value |
| `CADENCE_ADMIN_KEY_PREVIOUS` | - (empty) | previous key still accepted while clients catch up during rotation |
| `CADENCE_INTERNAL_PROXY_KEY` | - | separate 32+ character secret authenticating Caddy's transport-identity headers to the backend |
| `CADENCE_DASHBOARD_AUTH` | `on` | basic-auth gate at Caddy on the dashboard + read/admin API (agent endpoints exempt). `off` disables it |
| `CADENCE_DASHBOARD_USER` | `cadence` | basic-auth username |
| `CADENCE_DASHBOARD_PASSWORD_HASH` | - | bcrypt hash of the password, **with every `$` doubled** (`gen-secrets.sh` / `rotate-dashboard-password.sh` handle this; Caddy refuses to start on a malformed hash, and logs a warning at boot if the bcrypt cost is below 12, the generators use 14) |
| `CADENCE_SITE_ADDRESS` | `cadence.lan` | hostname Caddy serves and issues a cert for |
| `CADENCE_AGENT_PORT` | `8443` | dedicated HTTPS port requiring an agent client certificate |
| `CADENCE_API_DOCS_ENABLED` | `false` | serve `/docs`, `/redoc`, `/openapi.json` (behind basic-auth); `true` only in a dev `.env` |
| `CADENCE_OIDC_ENABLED` | `false` | operator sign-in via OIDC; inert unless issuer, client id/secret and redirect URI are all set. See [docs/oidc.md](docs/oidc.md) |
| `CADENCE_OIDC_ISSUER` / `CLIENT_ID` / `REDIRECT_URI` | - (empty) | required when enabled; issuer copied verbatim from the discovery document |
| `CADENCE_OIDC_CLIENT_SECRET` | - (empty) | required when enabled; back-channel code exchange only, never logged. No default by design |
| `CADENCE_OIDC_SCOPES` | `openid email profile` | requested scopes |
| `CADENCE_OIDC_SESSION_TTL_SECONDS` | `28800` (8 h) | fixed session lifetime, re-login after; no sliding renewal |
| `CADENCE_OIDC_CLOCK_SKEW_SECONDS` | `120` | leeway for `exp`/`iat` checks against provider clock drift |
| `CADENCE_OIDC_COOKIE_SECURE` | `true` | `Secure` flag on session cookies; exists so tests can run the flow over plain HTTP |
| `CADENCE_LEGACY_AGENT_ENDPOINTS` | no implicit value | explicit `on` during host migration, then `off`; Compose refuses to start if unset |
| `CADENCE_ENROLLMENT_DEFAULT_TTL_MINUTES` | `30` | default one-time code lifetime; must be from 5 to 240 minutes |
| `CADENCE_HTTP_BIND` | `127.0.0.1` | interface for Caddy's 80/443/8443; set `0.0.0.0` to serve the LAN |
| `CADENCE_BACKEND_BIND` / `CADENCE_FRONTEND_BIND` | `127.0.0.1` | interface for the backend / plain-HTTP frontend ports; keep on loopback |
| `CADENCE_TRUSTED_PROXIES` | - (empty) | reverse-proxy networks (CIDRs) whose `X-Forwarded-For` is trusted for the auth throttle and audit `client`; empty = use the direct peer IP. Set to the compose network subnet, see [Recording the real client IP](#recording-the-real-client-ip) |
| `CADENCE_REPORTS_RETENTION_DAYS` / `CADENCE_JOBS_RETENTION_DAYS` | `90` | daily prune of `reports` / terminal `jobs`; `0` = keep forever |
| `CADENCE_BACKUP_DIR` | `<repo>/backups` | base directory for `scripts/backup.sh` output, relative to the server checkout (not a fixed system path like `/var/backups`) |
| `CADENCE_BACKUP_KEEP` | `14` | how many timestamped backup dirs to keep |
| `CADENCE_PACKAGES_GC_ENABLED` | `true` | weekly sweep deleting `packages` rows no host references anymore |
| `CADENCE_AUDIT_RETENTION_DAYS` | `365` | daily prune of the admin audit trail (`audit_log`); `0` = keep forever |
| `CADENCE_TOKEN_DEFAULT_EXPIRY_DAYS` | `365` | default `expires_at` for new agent tokens; pass an explicit date to differ |
| `CADENCE_TOKEN_ENCRYPTION_KEY` | - | required Fernet key resealing token secrets at rest; keep `.env` at mode `0600` |
| `CADENCE_TOKEN_ENCRYPTION_KEY_PREVIOUS` | - (empty) | previous Fernet key still accepted for decryption during rotation |
| `CADENCE_TOKEN_RETENTION_DAYS` | `90` | days since revocation/expiry before a dead token row is deleted; `0` = keep forever |
| `CADENCE_SIGNATURE_WINDOW_SECONDS` | `300` | max drift between `X-Cadence-Timestamp` and server clock; bounds replay, not a nonce |
| `CADENCE_RATELIMIT_ENABLED` | `true` | master switch for the successful-traffic rate limiter (auth failures use the separate throttle) |
| `CADENCE_RATELIMIT_WINDOW_SECONDS` | `60` | counting window for the caps below |
| `CADENCE_RATELIMIT_AGENT_MAX` / `ADMIN_MAX` / `DASHBOARD_MAX` / `ENROLLMENT_MAX` | `20` / `60` / `120` / `10` | max successful requests per window per key (agent token hash / admin IP / dashboard IP / enrollment peer) |
| `CADENCE_JOB_RUNNING_TIMEOUT_SECONDS` | `7200` | a job stuck `running` longer than this is failed by the scheduler; `0` = off |
| `CADENCE_MAX_REPORT_BYTES` / `CADENCE_MAX_REPORT_PACKAGES` | `5242880` (5 MiB) / `10000` | caps on a report body (stored verbatim); `0` disables that check. See [SECURITY.md](SECURITY.md) |
| `CADENCE_ADVISORY_REFRESH_ENABLED` | `true` | scheduler pull of the Debian DSA/DLA feeds; `false` for CI / air-gapped installs |
| `CADENCE_ADVISORY_FEED_URLS` | built-in Debian defaults | space/comma-separated feed override; empty = defaults in `app.advisories.debian` |
| `CADENCE_CVE_SCORE_REFRESH_ENABLED` | `true` | scheduler resolution of cached CVSS scores from the NVD; `false` for CI / air-gapped installs |
| `CADENCE_NVD_API_KEY` | - (empty, optional) | raises the NVD rate allowance; sent as `apiKey` header, never logged or stored |
| `CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES` | `1073741824` (1 GiB) | minimum free space for the filesystem backing `/var` before and after an upgrade |
| `CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES` | `209715200` (200 MiB) | minimum free space for filesystems backing `/boot` and `/boot/efi`; missing paths are ignored |
| `CADENCE_UPGRADE_LOCK_WAIT_SECONDS` | `120` | how long a pre-check waits for apt/dpkg advisory locks to clear; maximum 3600 |
| `CADENCE_WEBHOOKS_ENABLED` | `true` | master switch for outbound webhooks (inert until one is configured on the dashboard); `false` hard-disables enqueue + dispatch. See [Webhooks](#webhooks) and [docs/webhooks.md](docs/webhooks.md) |
| `CADENCE_WEBHOOK_TIMEOUT_SECONDS` / `CADENCE_WEBHOOK_MAX_ATTEMPTS` | `10` / `6` | per-attempt HTTP timeout; delivery attempts before a row is parked `failed` (backoff 60 s ... 1 h) |
| `CADENCE_WEBHOOK_DISPATCH_BATCH` | `20` | pending deliveries drained per scheduler tick |
| `CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS` | `900` | `last_seen_at` age before a `host.offline` webhook fires (once, re-arms on the next report) |
| `CADENCE_WEBHOOK_DELIVERIES_RETENTION_DAYS` | `30` | daily prune of terminal `webhook_deliveries` rows; `0` = keep forever |
| `CADENCE_WEBHOOK_LOG_MAX_BYTES` | `4096` | cap on the job log embedded in `job.*` payloads (head + tail kept); `0` = full log |
| `CADENCE_CAMPAIGN_OBSERVATION_WINDOW_SECONDS` | `600` | default quiet time between a campaign's stages (per-campaign overridable); `0` = advance as soon as a stage's jobs finish. See [docs/campaigns.md](docs/campaigns.md) |
| `CADENCE_MAX_CONCURRENT_REBOOTS` | `0` | fleet-wide cap on Cadence-driven reboots awaiting proof, across campaigns, schedules and manual jobs; `0` = off. See [docs/campaigns.md](docs/campaigns.md#reboots) |
| `CADENCE_CAMPAIGN_RETURN_TIMEOUT_SECONDS` | `1800` | how long a campaign waits for a rebooted host to prove its return before halting; strictly positive. See [docs/campaigns.md](docs/campaigns.md#reboots) |
| `CADENCE_LOG_LEVEL` | `INFO` | backend + scheduler log level (output is logfmt) |
| `CADENCE_DB_WAIT_SECONDS` | `60` | how long the entrypoint waits for Postgres before giving up |
| `CADENCE_SCHEDULER_HEARTBEAT_TIMEOUT` | `180` | scheduler healthcheck: max age of the loop's heartbeat |

### Agent (`/etc/cadence/agent.env`)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CADENCE_SERVER_URL` | yes | - | agent base URL, normally `https://cadence.lan:8443` after enrollment |
| `CADENCE_TOKEN` | yes | - | per-host HMAC secret, written from the one-time enrollment response |
| `CADENCE_CLIENT_CERT_FILE` / `CADENCE_CLIENT_KEY_FILE` | enrolled hosts | - | versioned mTLS client certificate chain and locally generated private key |
| `CADENCE_SERVER_CA_FILE` | enrolled hosts | - | fingerprint-verified server CA used for TLS validation |
| `CADENCE_RUN_APT_UPDATE` | no | `true` (installer) | run `apt-get update` before collecting; failure is non-fatal |
| `CADENCE_ENABLE_UPGRADES` | no | `true` | kill-switch: if `false`, a triggered job is reported `failed` |
| `CADENCE_ENABLE_REBOOT` | no | `true` | kill-switch: if `false`, never reboot even under an `auto` policy |
| `CADENCE_HTTP_TIMEOUT_SECONDS` | no | `30` | HTTP client timeout |
| `CADENCE_LOG_LEVEL` | no | - (unset) | anything but `debug` logs at info and above; `debug` enables debug lines (timestamps come from journald) |

## Operations

### Applying updates (jobs)

```
dashboard --POST /admin/hosts/{id}/jobs (X-Admin-Key)--> job: pending
agent     --POST /agent/next-job (every ~1 min)-------->  claims it, job: running
          (or delivered on the response to POST /reports)
agent     runs blocking pre-checks-------------------->  disk, locks, dpkg, apt, services
agent     runs apt-get dist-upgrade -y (non-interactive)
agent     runs post-checks---------------------------->  host health
agent     --POST /jobs/{id}/result-------------------->  action result + health + log
```

One active job per host (`409` otherwise). The dashboard prompts for the
`X-Admin-Key` once per session. Impatient? `systemctl start
cadence-agent.service` on the host runs a report (and any pending job) now.

A failed or unavailable blocking pre-check prevents apt from running. Once the
pre-check phase passes, post-checks run even if the action fails. Consequently,
job status (`succeeded` / `failed`) says whether the requested action completed,
while host health (`healthy` / `degraded` / `unhealthy` / `unknown`) says what
the agent could establish about the machine afterwards. Full check order,
severity and compatibility notes:
[docs/health-checks.md](docs/health-checks.md).

### Reboots

Each host has a `reboot_policy`: `never` (default, the agent only reports
`reboot_required`), `auto` (reboot when an upgrade left one pending), or
`prompt` (a `reboot` job is queued for you to run from the dashboard). Set it in
the host detail pane or `PATCH /api/v1/admin/hosts/{id}` with
`{"reboot_policy":"auto"}`. A job can override it via `params.reboot`.

The agent reboots only when the upgrade succeeded, a reboot is actually
required, the effective mode is `auto`, and `CADENCE_ENABLE_REBOOT` is not
`false`. It posts the job result first, so a job never hangs in `running`.

`reboot_required` is detected from `/var/run/reboot-required` (created by
`update-notifier-common` on Ubuntu / Debian ≤12, `reboot-notifier` on Debian
13) **or**, agent ≥0.6.0, by an installed kernel image of the running flavour
being newer than the running kernel (no helper package needed).

### Maintenance windows

One recurring window per host (`schedules` table). The `scheduler` service wakes
every minute and turns a due window into an `apt_upgrade` job. Manage it in the
host detail pane, or:

```sh
curl -s -X POST https://<site>/api/v1/admin/hosts/<id>/schedules \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"kind":"weekly","weekday":6,"hour":4,"minute":0,"timezone":"Europe/Paris","params":{"reboot":"auto"}}'
```

`weekly` (`weekday` 0–6, Monday = 0) or `monthly` (`day_of_month` 1–28), at
`hour`:`minute` in `timezone`. Ranges and `params.reboot` are enforced by DB
`CHECK`s. If the host already has an active job when the window opens, that run
is skipped (no catch-up).

### Campaigns

A **campaign** rolls an `apt_upgrade` across many hosts in ordered waves. Pick
the hosts once (an explicit list or a `tag` filter), split them into `stages`
(`[2, "25%", "rest"]`), and set `max_concurrency`, a `max_failures` budget and
an observation window between waves. It is created as a `draft` and started
with a separate call:

```sh
curl -s -X POST https://<site>/api/v1/admin/campaigns \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"name":"march","tag":"env=prod","stages":[2,"25%","rest"],
       "max_concurrency":5,"max_failures":3}'
curl -s -X POST https://<site>/api/v1/admin/campaigns/<id>/activate -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
```

The `scheduler` advances every running campaign each tick: it reconciles
finished jobs (a failed job's category maps to `skip` or `halt`; a successful
job must also report `healthy` or `degraded`), fills the active wave up to
`max_concurrency`, holds each terminal wave for its observation window, and
stops on a halt condition or once the skip count passes `max_failures`.
`pause` / `resume` / `cancel` are the manual controls; the `#campaigns`
dashboard section has the create form and a per-stage / per-host view. A
host that reboots on `auto` stays `running` until it proves its return
(new boot plus fresh acceptable health), and a global budget can cap
concurrent unproven reboots fleet-wide. Full
detail: [docs/campaigns.md](docs/campaigns.md).

Every target host must be on agent `0.12.0` or newer before you `activate` a
campaign. A successful job from an older agent carries no health result, which
the reconcile step reads as `health_unknown` and the campaign stops at that
host. Roll the fleet forward first ([Upgrading the agent
fleet](#upgrading-the-agent-fleet)); manual and scheduled jobs are unaffected.

### Database migrations

Alembic owns the schema and runs on container start, **a normal deploy has
nothing to run**:

```sh
git pull && scripts/deploy.sh
```

`scripts/deploy.sh` rebuilds and restarts the stack (`docker compose up -d
--build --wait`), prints the migration head, re-stages the agent bootstrap
assets so the served binary matches the checkout, and recreates Caddy so a
host-edited `Caddyfile` or the freshly staged assets take effect. `docker
compose up -d --build` on its own still works for a stack-only change but skips
the agent restage and the Caddy recreate.

A fresh database is built from revision `0001`. `backend/app/db/init.sql` is
kept only as a reference copy of that baseline. If you are adopting a database
that was bootstrapped from the old `init.sql` mount (baseline tables, no
`alembic_version`), the entrypoint stamps `0001` automatically before upgrading.
Full detail: [docs/architecture.md](docs/architecture.md#deployment-notes).

### Backup & restore

Three things are irreplaceable on the server: the Postgres database, Caddy's
server-PKI volume and the backend-only client-PKI volume. Losing either PKI
requires fleet re-enrollment.

```sh
scripts/backup.sh
# -> backups/<UTC timestamp>/{db.dump, caddy_data.tgz, client_pki.tgz, env, MANIFEST}
```

Server backups never contain signing keys (the active key lives only as a CI
secret, the backup key is kept offline), see
[Signed agent releases](#signed-agent-releases).

`CADENCE_BACKUP_DIR` / `CADENCE_BACKUP_KEEP` (default 14) tune it. Run it
nightly from cron. `scripts/restore-check.sh [dir]` restores the newest (or
given) backup into throwaway containers, asserts it loads, both PKIs validate
and no signing-key material is present, and tears them down without touching
the live stack.

To restore for real, from a checkout at the commit in `MANIFEST`:

```sh
B=backups/<timestamp>
cp "$B/env" .env

docker compose up -d db
until docker compose exec -T db pg_isready -U cadence -d cadence; do sleep 1; done
docker compose exec -T db pg_restore -U cadence -d cadence --clean --if-exists < "$B/db.dump"

docker run --rm -v cadence_caddy_data:/v -v "$PWD/$B":/b:ro postgres:16 \
  sh -c 'tar xzf /b/caddy_data.tgz -C /v'
docker run --rm -v cadence_client_pki:/v -v "$PWD/$B":/b:ro postgres:16 \
  sh -c 'tar xzf /b/client_pki.tgz -C /v'

docker compose up -d
docker compose run --rm backend alembic current    # matches MANIFEST
```

Volume names are `<project>_pgdata`, `<project>_caddy_data` and
`<project>_client_pki` (`project` = the repo directory name). Restoring onto a
stack with data needs `docker compose down -v` first, destructive, back up
immediately before.

### Upgrading the agent fleet

Tag the release so the built binary reports the right version:
`git tag -a agent-v0.15.0 -m 'agent 0.15.0'` on the commit matching the newest
`agent/CHANGELOG.md` heading (the workflow fails if that `## 0.15.0` section is
missing), and push the tag to the repository that runs the release workflow.
CI builds and signs the release and creates a draft; once you have verified the
`.minisig` files and published the draft, stage it on the server (the version is
stamped via `-ldflags` from the tag name):

```sh
scripts/publish-agent.sh fetch 0.15.0
```

The break-glass path, for a release built by hand with the offline key, is
`scripts/build-agent-release.sh` followed by `scripts/publish-agent.sh publish
<version> <dir>`; see [Signed agent releases](#signed-agent-releases). Either way
the hosts then pick the version up through an `agent_upgrade` campaign (see
[docs/agent-upgrade-campaigns-runbook.md](docs/agent-upgrade-campaigns-runbook.md)).

`scripts/deploy.sh` does not publish agent versions; it only restages the
bootstrap assets.

**Hosts older than 0.15.0 need the installer once.** An agent up to `0.14.0`
does not know `agent_upgrade`: it fails such a job with `unsupported job type`,
and the server does not check the agent version for you. Some of those hosts
also still run the binary from `/usr/local/bin`, which `agent_upgrade` refuses.
Such a host goes through the installer one time, and campaigns work from then
on. The installer is downloaded over HTTPS with the CA the host already pinned,
saved to a file and run as root with `CADENCE_UPGRADE_ONLY=true`:

```sh
curl -fsSL --cacert /usr/local/share/ca-certificates/cadence-server.crt \
  https://<site>/install.sh -o install.sh
sudo CADENCE_BASE_URL=https://<site> \
  CADENCE_VERIFIED_CA=/usr/local/share/ca-certificates/cadence-server.crt \
  CADENCE_UPGRADE_ONLY=true sh install.sh
```

This mode refuses to run unless the host is already enrolled, preserves its HMAC
token and mTLS credentials, installs the binary at `/usr/bin/cadence-agent` and
refreshes the units. Add `CADENCE_MINISIGN_PUB` (and `CADENCE_MINISIGN_PUB2`) to
that command to check the binary's signature as described above. Afterwards
`scripts/check-agent-host-upgrade-readiness.sh` should print `6B-READY`;
[docs/agent-path-migration.md](docs/agent-path-migration.md) covers the other
historical states. Never fetch or execute the installer over HTTP. See also
`agent/CHANGELOG.md` and [docs/enrollment-mtls.md](docs/enrollment-mtls.md).

### Rotating a host's agent token

Tokens live in `agent_tokens`; a host can have several active at once. Rotate
roll-forward so the host never has a gap:

```sh
# 1. issue a new token (optional label / future expires_at)
curl -s -X POST https://<site>/api/v1/admin/hosts/<id>/tokens \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"label":"rotation 2026-09"}'                      # -> {"id":..,"token":".."}

# 2. put the new token in /etc/cadence/agent.env on the host, restart the agent
# 3. confirm it is the one being used, then revoke the old token by its id
curl -s https://<site>/api/v1/admin/hosts/<id>/tokens -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
curl -s -X DELETE https://<site>/api/v1/admin/hosts/<id>/tokens/<old_token_id> \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
```

The list shows `created_at`, `last_used_at` and a `state` of
`active` / `expired` / `revoked` for each token. Revoking is auth-plane only,
it does not cancel a job already queued or running (deactivate the host for
that).

### Retention

Covered by `CADENCE_*_RETENTION_DAYS` above; the sweep runs once a day in the
scheduler (`retention sweep: …` in `docker compose logs scheduler`).
`host_packages` is replaced wholesale on every report, so it does not grow.

### Audit trail

Every successful admin write (host create/patch/delete, job queue/clear,
schedule create/update/delete) is logged. Read it with the admin key, newest
first; page with `before` + `before_id` from the last row, and filter on
`action` / `target_type` / `target_id`:

```sh
curl -s https://<site>/api/v1/admin/audit -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
```

Callers may set `X-Actor: alice` on their writes to stamp the `actor` column
(it defaults to `admin`, the shared key proves no identity on its own).

### Self-monitoring

Beyond `/healthz` (liveness) and `/readyz` (database reachability),
`GET /api/v1/metrics` exposes Prometheus-text gauges for an external
scraper: job counts by status and type, campaigns by status, webhook
delivery backlog and recent failures, hosts by freshness bucket and
unhealthy count, agent versions across the fleet, and the scheduler
heartbeat age. It sits behind the same basic-auth as the dashboard reads
(agent endpoints stay exempt); a dead database still answers 200 with only
`cadence_db_up 0`, since scrapers ignore non-2xx bodies:

```yaml
scrape_configs:
  - job_name: cadence
    basic_auth: {username: cadence, password: <dashboard password>}
    static_configs: [{targets: ["cadence.lan:443"]}]
    metrics_path: /api/v1/metrics
    scheme: https
```

### Webhooks

Cadence can POST a signed JSON body to an endpoint you control when a job
finishes, a host goes offline, a host starts needing a reboot, or a host gains
pending security updates. Configure it in the dashboard's **Webhooks** section
(the full URL and the signing secret are shown once, at creation), or through
`POST /api/v1/admin/webhooks`.

The body is one generic shape for every event and receiver: it is **not** the
format Discord, Slack or Teams expect, so a webhook pointed straight at a chat
URL fails every delivery. [docs/webhooks.md](docs/webhooks.md) has the payload
of each event, a signature-verification receiver to copy, and how to relay to a
chat tool.

### Recording the real client IP

The auth-failure throttle and the audit `client` column use the caller's IP.
The backend sits behind Caddy + nginx, so out of the box that IP is always the
nginx container's, `X-Forwarded-For` is **not** trusted by default (a client
could forge it). To record the real client, set `CADENCE_TRUSTED_PROXIES` to
the compose network subnet:

```sh
docker network inspect cadence_default \
  --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'   # e.g. 172.18.0.0/16
# in .env:
CADENCE_TRUSTED_PROXIES=172.18.0.0/16
docker compose up -d backend                            # picks up the change
```

With it set, `X-Forwarded-For` is walked right-to-left, trusted hops are
skipped, and the first address outside the trusted networks is used. Leave it
empty on any setup where the backend is reachable without going through the
bundled proxies.

## Development

```sh
# agent
docker run --rm -v "$PWD/agent":/s -w /s golang:1.27 \
  sh -c 'go vet ./... && go test ./... && CGO_ENABLED=0 go build -o /dev/null ./cmd/agent'

# backend (needs the db service; pytest runs against a real PostgreSQL)
docker compose up -d db
docker compose run --rm -v "$PWD/backend:/app" backend \
  sh -c 'pip install -q -r requirements-dev.txt && pytest -q'

# frontend
cd frontend && npm ci && npm run lint && npm test && npm run build
npm run dev            # http://localhost:5173, proxies /api to http://localhost:8000
```

CI runs the matching suites on every push (jobs are path-filtered); the full
`docker compose` bring-up (`stack` job) runs on `main` and merge requests.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

A single shared basic-auth credential gates the dashboard and read/admin API
(on by default; agent endpoints are exempt), Caddy binds to loopback by
default, and `CADENCE_ADMIN_KEY` authorizes writes (so does an operator's OIDC
session, see below). Successful admin
writes are recorded in an audit trail (`GET /api/v1/admin/audit`), though the
shared key means it cannot attribute them to a real user. Operators can
alternatively sign in via OIDC (optional, see [docs/oidc.md](docs/oidc.md));
their writes audit under the verified subject. Agent tokens
(`agent_tokens`) can be rotated, given an expiry and revoked. There is no
user management beyond two OIDC roles (an account in
`CADENCE_OIDC_OPERATOR_EMAILS` is an operator, any other signed-in account is a
reader; the shared admin key bypasses roles), and agents fully trust the server.
Before exposing Cadence beyond a network you control, read
**[SECURITY.md](SECURITY.md)**. Report vulnerabilities privately (same file).

## License

[Apache-2.0](LICENSE).
