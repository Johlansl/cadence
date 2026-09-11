# Cadence

**Lightweight patch management for Linux servers.** A small Go agent reports
what's installed and what updates are pending; a web dashboard shows the fleet
at a glance and lets you trigger `apt` upgrades (and reboots) per host, on
demand or on a schedule.

Cadence aims to sit between tools that are too heavy for a small fleet or a
homelab (RHEL Satellite / Uyuni: many GB of RAM, an imposed host OS, mandatory
DNS) and tools that only show you the problem without fixing it. It gives you
**visibility and remediation** in one place, on a 2 vCPU / 2 GB box.

> **Status: v1, single-operator.** It works and is in real use. There is no
> multi-user auth or RBAC; a single shared basic-auth credential gates the
> dashboard and read/admin API (on by default), and agents fully trust the
> server. Read [SECURITY.md](SECURITY.md) before exposing it beyond a trusted
> network.

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
  resulting host health.
- **Reboots**: per-host policy (`never` / `auto` / `prompt`), overridable per
  job; the agent reboots only when the upgrade actually left one pending.
- **Maintenance windows**: one recurring weekly/monthly window per host,
  turned into jobs by a scheduler service.
- **Campaigns**: roll a dist-upgrade across many hosts in ordered waves, with
  a global concurrency cap, an observation pause between waves, and an
  automatic stop when failures pile up.
- **History & retention**: append-only report and job logs, pruned on a
  configurable schedule.

Not in v1 (see [docs/decisions.md](docs/decisions.md#v1-scope)): package
exclusion lists, multi-host reboot orchestration, notifications, non-apt
package managers, multi-user / RBAC.

## How it fits together

```
                          monitored hosts
                     ┌───────────────────────┐
                     │  cadence-agent (Go)   │   systemd timers:
                     │  one-shot, stdlib     │   ~30 min report, ~1 min job poll
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
echo 'CADENCE_VERSION=0.2.0' >> .env
docker compose -f docker-compose.yml -f docker-compose.release.yml pull
docker compose -f docker-compose.yml -f docker-compose.release.yml up -d
```

Each image carries a Sigstore build-provenance attestation:

```sh
gh attestation verify oci://ghcr.io/johlansl/cadence-backend:0.2.0 --repo Johlansl/cadence
```

`scripts/deploy.sh` on the central server always builds from source and ignores
this overlay.

## Add a monitored host

**1. Register it** (on the server, reads `CADENCE_ADMIN_KEY` from `.env`):

```sh
scripts/provision-host.sh vm-web-01 "web frontend"
```

This creates the host, prints the one-time token, and prints the exact install
command for the host. To do it by hand:

```sh
curl -s -X POST https://<site>/api/v1/admin/hosts \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"hostname":"vm-web-01","description":"web frontend"}'
# -> {"id":"...","hostname":"vm-web-01","token":"..."}   keep the token
```

**2. Stage the agent assets** (once per server, re-run after each agent release):

```sh
scripts/publish-agent.sh          # builds the agent into ./dist, served by Caddy over HTTP
```

**3. Install on the host**, as root, `provision-host.sh` prints this line:

```sh
curl -fsSL http://<site>/install.sh | sudo CADENCE_TOKEN=<token> sh
```

The installer trusts the internal CA, installs a reboot-required helper if one
is available, drops the binary and `systemd` units, writes
`/etc/cadence/agent.env` (mode 0600), and enables all three timers. The installer and
binary are fetched over plain HTTP (the host doesn't trust the CA yet) and the
binary is checksum-verified, see [SECURITY.md](SECURITY.md) for the trust
model. The existing token in `agent.env` is preserved on re-run, so the same
command upgrades an already-installed agent.

**Manual install** (no `curl | sh`): trust the CA (below), build the static
binary (`cd agent && CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent
./cmd/agent`), copy `bin/cadence-agent` + `agent/systemd/` to the host, run
`sudo systemd/install.sh`, then edit `/etc/cadence/agent.env`
(`CADENCE_SERVER_URL`, `CADENCE_TOKEN`).

Verify:

```sh
sudo systemctl start cadence-agent.service
journalctl -u cadence-agent -n 20 --no-pager        # "report sent to ..."
curl -s https://<site>/api/v1/hosts                  # the host appears, last_seen_at fresh
```

### Install from a `.deb`

Each `agent-v*` release also ships a `.deb` (`amd64` + `arm64`) as a Release
asset, an `apt`-native alternative to `curl | sh` for hosts you manage with a
configuration tool. It installs the binary and the `systemd` units but
**deliberately does not** trust a CA or write the per-host token, so you still
do those two steps yourself:

```sh
VER=0.7.2; ARCH=amd64
curl -fsSLO "https://github.com/Johlansl/cadence/releases/download/agent-v$VER/cadence-agent_${VER}-1_${ARCH}.deb"
curl -fsSLO "https://github.com/Johlansl/cadence/releases/download/agent-v$VER/cadence-agent_${VER}-1_${ARCH}.deb.sha256"
sha256sum -c "cadence-agent_${VER}-1_${ARCH}.deb.sha256"
gh attestation verify "cadence-agent_${VER}-1_${ARCH}.deb" --repo Johlansl/cadence   # optional

sudo apt install "./cadence-agent_${VER}-1_${ARCH}.deb"
# then, as the post-install message says:
#   1. trust the server CA (see below)
#   2. sudo install -D -m 0600 /usr/share/doc/cadence-agent/agent.env.example /etc/cadence/agent.env
#      and set CADENCE_SERVER_URL + CADENCE_TOKEN (from scripts/provision-host.sh)
#   3. sudo systemctl start cadence-agent.timer cadence-agent-poll.timer
```

`apt upgrade` then moves the agent forward on the next release. It is not served
from an apt repository, download the `.deb` from the Release.

### Trust Caddy's internal CA on a host

```sh
# on the server, stack up:
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > cadence-ca.crt
# on the host:
sudo cp cadence-ca.crt /usr/local/share/ca-certificates/
sudo update-ca-certificates
```

The one-liner installer does this for you.

### Signed agent releases

The installer fetches the agent over plain HTTP and checks a SHA-256 sum, that
catches a truncated download but not tampering. `scripts/publish-agent.sh`
[minisign](https://jedisct1.github.io/minisign/)-signs every release
(`cadence-agent.minisig` beside the binary); with `agent/minisign.pub`
committed it refuses to publish unsigned.

For tamper-evidence at install time, install `minisign` on the host first
(`apt-get install -y minisign` on Debian/Ubuntu) and pass the public key,
**out of band**, not over the install channel, to the installer:

```sh
curl -fsSL http://cadence.lan/install.sh \
  | sudo CADENCE_TOKEN=<token> CADENCE_MINISIGN_PUB="$(cat agent/minisign.pub)" sh
```

A missing or invalid signature then aborts the install; if `minisign` is not
installed the installer stops and tells you to install it. Without
`CADENCE_MINISIGN_PUB` the installer uses the SHA-256 check only, the default,
no `minisign` needed, sufficient for the trusted-LAN target.

Back up the (passwordless) signing key with `scripts/backup-signing-key.sh`, it
writes a passphrase-protected copy that `scripts/backup.sh` then includes in
every backup; `scripts/restore-signing-key.sh` restores it. Key rotation is
written up in
[docs/decisions.md](docs/decisions.md#agent-distribution--signing).

The pre-built binaries attached to each GitHub **Release** (`agent-v*` tag) are
a separate channel, not minisign-signed (the fleet key never touches CI), but
each carries a Sigstore build-provenance attestation:

```sh
gh release download agent-v0.7.1 --repo Johlansl/cadence -p 'cadence-agent-linux-amd64*'
sha256sum -c cadence-agent-linux-amd64.sha256
gh attestation verify cadence-agent-linux-amd64 --repo Johlansl/cadence
```

**Running your own Cadence?** Replace `agent/minisign.pub` with your own key
(`minisign -G -W -p agent/minisign.pub -s ~/.cadence/minisign.key`, keep the
secret key off the repo) or delete it to publish unsigned. See
[docs/decisions.md](docs/decisions.md#agent-distribution--signing).

## Configuration

All configuration is environment variables. Server variables live in `.env`
(read by `docker compose`); agent variables live in `/etc/cadence/agent.env`.

### Server (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `POSTGRES_USER` / `POSTGRES_DB` | `cadence` / `cadence` | database role and name |
| `POSTGRES_PASSWORD` | - | **read only on first boot** of the `pgdata` volume; changing it later needs `down -v` or an `ALTER ROLE` |
| `CADENCE_ADMIN_KEY` | - | shared secret for every admin write (`X-Admin-Key`). Use a strong value |
| `CADENCE_DASHBOARD_AUTH` | `on` | basic-auth gate at Caddy on the dashboard + read/admin API (agent endpoints exempt). `off` disables it |
| `CADENCE_DASHBOARD_USER` | `cadence` | basic-auth username |
| `CADENCE_DASHBOARD_PASSWORD_HASH` | - | bcrypt hash of the password, **with every `$` doubled** (`gen-secrets.sh` / `rotate-dashboard-password.sh` handle this; Caddy refuses to start on a malformed hash, and logs a warning at boot if the bcrypt cost is below 12, the generators use 14) |
| `CADENCE_SITE_ADDRESS` | `cadence.lan` | hostname Caddy serves and issues a cert for |
| `CADENCE_HTTP_BIND` | `127.0.0.1` | interface for Caddy's 80/443; set `0.0.0.0` to serve the LAN |
| `CADENCE_BACKEND_BIND` / `CADENCE_FRONTEND_BIND` | `127.0.0.1` | interface for the backend / plain-HTTP frontend ports; keep on loopback |
| `CADENCE_TRUSTED_PROXIES` | - (empty) | reverse-proxy networks (CIDRs) whose `X-Forwarded-For` is trusted for the auth throttle and audit `client`; empty = use the direct peer IP. Set to the compose network subnet, see [Recording the real client IP](#recording-the-real-client-ip) |
| `CADENCE_REPORTS_RETENTION_DAYS` / `CADENCE_JOBS_RETENTION_DAYS` | `90` | daily prune of `reports` / terminal `jobs`; `0` = keep forever |
| `CADENCE_AUDIT_RETENTION_DAYS` | `365` | daily prune of the admin audit trail (`audit_log`); `0` = keep forever |
| `CADENCE_JOB_RUNNING_TIMEOUT_SECONDS` | `7200` | a job stuck `running` longer than this is failed by the scheduler; `0` = off |
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
| `CADENCE_LOG_LEVEL` | `INFO` | backend + scheduler log level (output is logfmt) |
| `CADENCE_DB_WAIT_SECONDS` | `60` | how long the entrypoint waits for Postgres before giving up |
| `CADENCE_SCHEDULER_HEARTBEAT_TIMEOUT` | `180` | scheduler healthcheck: max age of the loop's heartbeat |

### Agent (`/etc/cadence/agent.env`)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `CADENCE_SERVER_URL` | yes | - | backend base URL, e.g. `https://cadence.lan` |
| `CADENCE_TOKEN` | yes | - | a per-host token (from registration, or issued via `/api/v1/admin/hosts/{id}/tokens`) |
| `CADENCE_RUN_APT_UPDATE` | no | `true` (installer) | run `apt-get update` before collecting; failure is non-fatal |
| `CADENCE_ENABLE_UPGRADES` | no | `true` | kill-switch: if `false`, a triggered job is reported `failed` |
| `CADENCE_ENABLE_REBOOT` | no | `true` | kill-switch: if `false`, never reboot even under an `auto` policy |
| `CADENCE_HTTP_TIMEOUT_SECONDS` | no | `30` | HTTP client timeout |

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
dashboard section has the create form and a per-stage / per-host view. Full
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

Two things are irreplaceable on the server: the Postgres database and **Caddy's
data volume** (it holds the internal CA, lose it and every agent fails TLS
until re-provisioned).

```sh
scripts/backup.sh
# -> backups/<UTC timestamp>/{db.dump, caddy_data.tgz, env, minisign.key.enc, MANIFEST}
```

`minisign.key.enc` (the agent signing key, passphrase-protected) is included
only once `scripts/backup-signing-key.sh` has been run, see [Signed agent
releases](#signed-agent-releases).

`CADENCE_BACKUP_DIR` / `CADENCE_BACKUP_KEEP` (default 14) tune it. Run it
nightly from cron. `scripts/restore-check.sh [dir]` restores the newest (or
given) backup into throwaway containers, asserts it loads, the CA still
validates and the signing-key backup is encrypted, and tears them down without
touching the live stack.

To restore for real, from a checkout at the commit in `MANIFEST`:

```sh
B=backups/<timestamp>
cp "$B/env" .env

docker compose up -d db
until docker compose exec -T db pg_isready -U cadence -d cadence; do sleep 1; done
docker compose exec -T db pg_restore -U cadence -d cadence --clean --if-exists < "$B/db.dump"

docker run --rm -v cadence_caddy_data:/v -v "$PWD/$B":/b:ro postgres:16 \
  sh -c 'tar xzf /b/caddy_data.tgz -C /v'

docker compose up -d
docker compose run --rm backend alembic current    # matches MANIFEST
```

Volume names are `<project>_pgdata` / `<project>_caddy_data` (`project` = the
repo directory name). Restoring onto a stack with data needs `docker compose
down -v` first, destructive, back up immediately before.

### Upgrading the agent fleet

Tag the release first so the built binary reports the right version:
`git tag -a agent-v0.7.0 -m 'agent 0.7.0'` on the commit matching the newest
`agent/CHANGELOG.md` heading, and push the tag. `scripts/publish-agent.sh`
stamps that tag into the binary (`git describe`, via `-ldflags`); an untagged
build falls back to the `agentVersion` literal in `agent/cmd/agent/main.go`.

Then redeploy the server with `scripts/deploy.sh` (or run
`scripts/publish-agent.sh` alone if the stack is otherwise untouched). On each
host, re-run the one-liner (it preserves the token) or the manual build, and
`systemctl restart cadence-agent.service`. See `agent/CHANGELOG.md`.

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
docker run --rm -v "$PWD/agent":/s -w /s golang:1.23 \
  sh -c 'go vet ./... && go test ./... && CGO_ENABLED=0 go build -o /dev/null ./cmd/agent'

# backend (needs the db service; pytest runs against a real PostgreSQL)
docker compose up -d db
docker compose run --rm -v "$PWD/backend:/app" backend \
  sh -c 'pip install -q -r requirements-dev.txt && pytest -q'

# frontend
cd frontend && npm ci && npm run lint && npm test && npm run build
npm run dev            # http://localhost:5173, proxies /api to http://localhost:8000
```

CI runs the three suites plus a full `docker compose` bring-up on every push.
See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

A single shared basic-auth credential gates the dashboard and read/admin API
(on by default; agent endpoints are exempt), Caddy binds to loopback by
default, and `CADENCE_ADMIN_KEY` authorizes every write. Successful admin
writes are recorded in an audit trail (`GET /api/v1/admin/audit`), though the
shared key means it cannot attribute them to a real user. Agent tokens
(`agent_tokens`) can be rotated, given an expiry and revoked. There is no
multi-user auth and agents fully trust the server. Before exposing Cadence
beyond a network you control, read **[SECURITY.md](SECURITY.md)**. Report
vulnerabilities privately (same file).

## License

[Apache-2.0](LICENSE).
