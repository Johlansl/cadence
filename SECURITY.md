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
independent application-authentication layer. Minisign is an additional
release-signing check on the agent binary, not the root of first-contact trust;
what it does and does not cover is spelled out in
[Release signing](#release-signing) below.

The `.deb` packages and the container images attached to each GitHub Release
(`agent-v*` / `v*` tags) are **not** minisign- or GPG-signed. They carry a
Sigstore build-provenance attestation instead: `gh attestation verify
<file-or-oci-ref> --repo Johlansl/cadence` (see
[docs/decisions.md](docs/decisions.md#release-automation)). The `.deb` is for a
local `apt install ./…deb`, not an apt repository. The agent binaries of the same
release also carry a minisign signature, covered next.

### Release signing

**Who signs, and where.** Agent releases are signed in CI, by the `sign` job of
`.github/workflows/release.yml`, on an `agent-v*` tag. That job runs in the
GitHub environment `release` and is the only job that references the secret
`MINISIGN_SECRET_KEY`, the active key (`C9EDE89E1DEEED37`); the server jobs (`v*`
tags, GHCR images) never use that environment. The job re-checks both binaries
against the SHA-256 the build job reported, signs them, and verifies every
signature against the committed `agent/minisign.pub` before anything is
uploaded. `publish` then creates the GitHub Release as a **draft**; the
maintainer checks the `.minisig` files locally and publishes it by hand. The
server downloads the published release (`publish-agent.sh fetch`) and verifies it
against both trusted keys before staging a byte of it.

**The two keys.** The active key signs in CI. The backup key (`2167C9939EC0F4FC`)
is encrypted and kept offline by the maintainer; it signs only a break-glass
release (`scripts/build-agent-release.sh`) when the active key is lost or
compromised. Both public keys are committed (`agent/minisign.pub`,
`agent/minisign-backup.pub`) and compiled into the agent, which accepts a
signature from either.

**What the repository cannot enforce.** The workflow names the environment
`release`, but whether that environment exists, requires a reviewer and is
limited to `agent-v*` tags is a GitHub repository setting, not something the
workflow file can enforce. With no protection rules, the `sign` job would run
unattended on any pushed `agent-v*` tag. Anyone with admin access to the
repository (or a stolen admin session) can change those settings, add or replace
the secret, or alter the workflow in a tagged commit; a reviewer approving the
job is approving the code that runs with the key. Earlier versions of this
project kept the signing key off CI entirely for this reason. Putting it in CI
was a deliberate trade, written up in
[docs/decisions.md](docs/decisions.md#release-automation); the draft stage, the
manual check of the signatures and the offline backup key are what limit the
damage.

**What this does and does not protect.**

- A compromised Cadence server cannot produce a release the fleet accepts: it
  holds no private key and builds nothing. It can refuse to serve and delete
  artifacts. (It can still run `apt-get` jobs on the fleet, see
  [The server is fully trusted by every agent](#the-server-is-fully-trusted-by-every-agent);
  signing only covers replacing the agent binary.)
- **Rollback.** Through `agent_upgrade` a compromised server cannot take an
  enrolled fleet back to an older release: the agent itself refuses a target
  below the version it is running (`upgrade.Decide` returns `RefusedDowngrade`,
  applied in `agent/cmd/agent/main.go`; the backend does not check it) and it
  requires the downloaded binary's `-version` to print exactly the target.
  The installer path has its own check, in `CADENCE_UPGRADE_ONLY=true` mode,
  with no way around it. Before it downloads anything it reads the version the
  host runs, as the highest `-version` of the two agent paths this installer has
  ever used (`/usr/bin/cadence-agent` and the historical
  `/usr/local/bin/cadence-agent`, a closed list: no other path is ever read or
  executed). After the download it verifies the SHA-256 and the signature, only
  then runs the downloaded binary with `-version` (empty environment, 30 second
  timeout, output capped at 4096 bytes), and refuses to install a version below
  the one the host runs. It refuses, rather than guesses, when `minisign` or
  `CADENCE_MINISIGN_PUB` is missing, when neither path holds an agent, or when a
  path that exists is not a regular executable file (a symlink, a directory) or
  does not report a valid version. The cost of reading both paths: a stale,
  unreadable file at the historical path blocks the installer until it is removed
  (`docs/agent-path-migration.md`, section 6). Versions are compared on their three
  leading integers, so `0.15.0-3-gabc` counts as `0.15.0`, and an equal version is
  accepted (a repair reinstall). What this does not cover: a fresh install (not
  upgrade-only), which needs an enrollment code typed by hand. And, like the key
  itself (see the `install.sh` bullet below), this check only protects against a
  compromised server if `install.sh` reached the host out of band: the server
  serves `install.sh` too and could serve one without the check.
- Someone who has only a signing key cannot deliver anything to an enrolled
  fleet. Agents download upgrades from their own Cadence server, at a URL they
  derive locally, never from GitHub. To use a stolen key against a fleet the
  attacker also has to get a file staged on that server.
- Self-hosters who stage releases with `publish-agent.sh fetch` from GitHub are
  more exposed. If an attacker holds the active key and can also publish a
  release on the repository they fetch from, the server's check passes, because
  the key is in its trust set. Such a server must pull the commit that replaces
  the trust set (see below) before it fetches anything else.
- `install.sh` is served by the Cadence server, over HTTPS rooted in the CA the
  bootstrap pinned. A key given out of band (`CADENCE_MINISIGN_PUB`) therefore
  protects against a compromised server only if the installer itself reached the
  host out of band; a host that downloads `install.sh` from a compromised server
  runs whatever installer that server serves, including one that skips the check.
- The systemd units the installer fetches are **not** signed. Only the agent
  binary has a signature.
- Without `CADENCE_MINISIGN_PUB`, the installer checks only a SHA-256 that comes
  from the same server as the binary. That catches truncation and staging
  mistakes; it does not detect a malicious server.
- For a fleet that is already enrolled, the real protection is `agent_upgrade`.
  The agent verifies the downloaded binary against the trust set compiled into
  itself, which neither the server nor a job can change, then checks the SHA-256,
  the ELF architecture, and only then runs `-version`, before it replaces
  itself.

### If a signing key is compromised

Both procedures use what the code does: every agent already trusts both keys, so
a release signed by the key that is still good is accepted everywhere, and an
agent's trust set changes only by installing a new agent binary. They are
written from the real tooling; steps that are GitHub settings rather than code
are marked as such.

**The backup key is compromised or lost.** The active key still works, so the
normal release path does the job. The same procedure replaces the backup key
after any break-glass use (below): the key was decrypted in memory on a
connected machine to sign, so it should not be trusted afterwards.

1. Generate a new backup key pair offline, with a password
   (`minisign -G -f -p agent/minisign-backup.pub -s <offline path>`). Keep the
   old backup key's offline copy for now: it is still the only way to sign a
   break-glass release until the fleet trusts the new one.
2. In one commit, replace `agent/minisign-backup.pub` and `backupPubKeyText` in
   `agent/internal/upgrade/trust.go` (`go test ./...` in `agent/` fails if they
   differ), and add the `## <version>` section to `agent/CHANGELOG.md`.
3. Release as usual: tag, let CI sign with the active key (`sign` checks against
   `agent/minisign.pub`, which did not change), verify and publish the draft, then
   `publish-agent.sh fetch <version>` on a server checkout that has the new
   `agent/minisign-backup.pub`.
4. Roll the release out with an `agent_upgrade` campaign
   ([runbook](docs/agent-upgrade-campaigns-runbook.md)). Agents still on the old
   trust set accept it (signed by the active key); the new binary trusts the
   active key and the new backup key.
5. Once the fleet has the new trust set, replace the maintainer's offline copy:
   write the new key's encrypted backup with
   `scripts/backup-signing-key.sh <new key file>` (it checks the key against the
   trust set, so it works from step 2 on), store it offline, and retire the old
   one. Give the new backup key to hosts you bootstrap from now on
   (`CADENCE_MINISIGN_PUB2`).

**The active key is compromised.** Until step 7, nothing signed by the new active
key is accepted by hosts that run the old agent.

1. Stop CI signing at once: delete the secret `MINISIGN_SECRET_KEY` from the
   `release` environment. The `sign` job refuses to run with an empty secret, so
   no CI release can be signed. Also restrict or delete the `release`
   environment and review who has access to the repository (GitHub settings, not
   enforced by the repository).
2. Generate a new active key pair offline, without a password (CI cannot answer a
   prompt): `minisign -G -W -f -p agent/minisign.pub -s <offline path>`.
3. In one commit, replace `agent/minisign.pub` and `activePubKeyText` in
   `agent/internal/upgrade/trust.go`, and add the `## <version>` section to
   `agent/CHANGELOG.md`. Tag it `agent-v<version>` (the build tool needs that
   exact tag on the checked-out commit, `scripts/check-release-source.sh`) and
   push the commit and the tag as usual. The version is not used up. The release
   workflow starts, `build` runs, and `sign` fails at its "write signing key"
   step because the secret is empty (and if a secret were still there, at the
   check of the signatures against the new `agent/minisign.pub`); `publish` needs
   `sign`, so it is skipped and no Release is created. The Sigstore attestations
   `build` produces are the only trace.
4. Restore the backup key to a tmpfs, build and sign with it, then delete it:

   ```sh
   scripts/restore-signing-key.sh --to /dev/shm/backup.key <backup key>.enc
   CADENCE_SIGNING_KEY=/dev/shm/backup.key \
     scripts/build-agent-release.sh <version> /tmp/agent-<version>
   rm -f /dev/shm/backup.key
   ```

   `minisign` asks for the key's password itself (once per architecture). The
   build log must name the backup key (`2167C9939EC0F4FC`) as the one that
   verified.
5. Create the GitHub Release by hand from the bundle, so that servers that stage
   with `publish-agent.sh fetch` can get it. `fetch` downloads exactly six
   assets, named `cadence-agent-linux-<arch>`, `.sha256` and `.minisig` for
   `amd64` and `arm64`, and the bundle's files already carry these names and the
   same format as CI's (the `version` file in the bundle is not an asset: do not
   upload it). Create it as a draft, check it, then publish it:

   ```sh
   cd /tmp/agent-<version>
   gh release create agent-v<version> --repo Johlansl/cadence --verify-tag --draft \
     --title "agent <version>" \
     --notes "Break-glass release signed with the backup key. See agent/CHANGELOG.md." \
     cadence-agent-linux-amd64 cadence-agent-linux-amd64.sha256 cadence-agent-linux-amd64.minisig \
     cadence-agent-linux-arm64 cadence-agent-linux-arm64.sha256 cadence-agent-linux-arm64.minisig
   # download the draft into an empty directory (gh must be logged in with access
   # to the repository, drafts are not public) and check, for each arch, against
   # the BACKUP key:
   #   sha256sum -c cadence-agent-linux-<arch>.sha256
   #   minisign -V -p agent/minisign-backup.pub -m cadence-agent-linux-<arch> \
   #     -x cadence-agent-linux-<arch>.minisig
   # then publish it:
   gh release edit agent-v<version> --repo Johlansl/cadence --draft=false
   ```

   Such a release has no `.deb` and no Sigstore attestation: the workflow did not
   build it.
6. On the server, pull the commit and run `scripts/publish-agent.sh fetch
   <version>`; or, for a server that cannot reach GitHub, transfer the bundle and
   run `scripts/publish-agent.sh publish <version> /tmp/agent-<version>`.
7. Run the `agent_upgrade` campaign for the whole fleet. Only hosts that have
   installed this release trust the new active key. Do not rely on a CI release
   (signed by the new key) reaching a host before that.
8. Install the new key (the whole key file) as `MINISIGN_SECRET_KEY` in the
   `release` environment once its protection is back in place, then delete the
   private key file that was generated offline: the active key lives only in the
   GitHub secret. Give the new active key to hosts you bootstrap from now on
   (`CADENCE_MINISIGN_PUB`, with `CADENCE_MINISIGN_PUB2` for the backup key).
9. Replace the backup key, with the procedure above: the one used here was
   decrypted in memory on a connected machine. Do it once the fleet runs the new
   trust set and CI signs again; it is an ordinary CI release, then a campaign,
   then the update of the maintainer's offline copy.

**A planned rotation of the active key** (no compromise) is this same
procedure, from step 2 on; deleting the secret first is optional. It cannot be
done any other way: the CI `sign` job verifies its signatures against the
`agent/minisign.pub` of the commit it builds, so it can only sign with the key
that file names, and `build-agent-release.sh` does the same for the keys of its
own commit. A release that removes a key from the trust set therefore cannot be
signed with that key, and an agent's trust set changes only through a new binary
that it already trusts. The backup key is the one that can sign it.

**What none of this can do.** A host that is unreachable keeps its old trust
set, compromised key included, until it installs a release: it would still accept
something signed with that key. Such a host is repaired by running the installer
again with the new keys (`CADENCE_UPGRADE_ONLY=true`). And a compromised key is
only dangerous to a fleet if the attacker can also get a file staged on its
Cadence server (above); the server-side check by `publish-agent.sh` uses the
committed trust set, so pull the commit that changes it first.

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
the agent is a static Go binary (standard library plus one pinned
signature-verification module) with no inbound network input (communication is
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
