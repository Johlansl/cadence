# Agent upgrade campaigns: production runbook

Fleet rollout of `agent_upgrade` through Campaigns (6B). Read
`docs/agent-path-migration.md` first: no host may enter a campaign that
is not PR7-ready.

## Preconditions (all mandatory)

- Every targeted host runs an agent that knows `agent_upgrade` (PR6),
  from `/usr/bin/cadence-agent`, with effective systemd units pointing
  at the canonical path. Verify per host:
  `sh scripts/check-agent-host-upgrade-readiness.sh` prints `6B-READY`.
- The target release was built and signed offline by the key holder
  (`scripts/build-agent-release.sh`), then staged with
  `scripts/publish-agent.sh publish`. Never sign on the server.
- Served artifacts verified: binary, `.sha256`, `.minisig` under
  `/agent/vN.N.N/` match the key-holder bundle.
- `CADENCE_UPGRADE_PROOF_TIMEOUT_SECONDS` is non-zero (default 600;
  minimum 420 enforced at startup). Never pair a zero proof timeout
  with a disabled generic reaper: silent jobs would never surface.
- Operator knows the `.prev` recovery below and has normal access
  (systemd, journal, DB read) to every targeted host.

## Recommended canary: a separate campaign

Prefer a standalone canary campaign over relying on stage 1 of a big
campaign. A separate campaign is an explicit operator gate: observe,
validate, then launch the fleet.

- 1 representative, non-critical host (`host_ids`, not a tag).
- `job_type: agent_upgrade`, `job_params: {"target_version": "N.N.N"}`.
- `stages: [1]`, `max_concurrency: 1`, `max_failures: 0`.
- `observation_window_seconds`: long enough for the environment
  (at least several poll cycles; 300+ on a quiet fleet).
- Activate via the API, watch it reach `completed`, then inspect the
  host: canonical `-version`, `.prev`, credentials untouched, fresh
  authenticated polls.

With `max_failures: 0` even a host-local skip stops the campaign:
nothing proceeds on an unproven release.

## Fleet campaign (after a green canary)

- New campaign, same `target_version`, explicit host list or tag.
- `max_concurrency`: moderate for the environment (there is no magic
  universal value; each running upgrade holds its slot until the
  version proof lands).
- `max_failures`: explicit small budget for host-local skips.
- Watch for `stopped` with a systemic halt reason: stop and diagnose,
  do not recreate blindly.

## Failure runbook (V1 dispositions)

- `agent_refused`: host-local (non-canonical path, upgrades disabled,
  bad local version, unsupported platform). Fix the host out of band;
  skipped, counts toward `max_failures`.
- `upgrade_install_failed`: host-local (disk, permissions, slow host
  hitting the 300 s local deadline). The canonical binary is intact
  (all install errors are pre-commit). Inspect, fix, re-target later.
- `upgrade_download_failed`: campaign **halts**. Publish or network
  problem (possibly fleet-wide: missing artifact). Verify the
  published bundle and serving path before any new campaign.
- `upgrade_verification_failed`: campaign **halts**. Treat the release
  as invalid or compromised until proven otherwise (checksum,
  signature, key id, ELF/arch, `-version` mismatch). Never relaunch
  blindly.
- `upgrade_proof_timeout`: campaign **halts**. The host may already
  have the new binary installed but silent (it never proved). Inspect
  the host first: canonical version, `.prev`, connectivity, journal.
  Then decide per host (healthy-but-slow vs recovery).
- `agent_lost`: host-local (host silent). Skipped; re-target when back.
- unknown/NULL category: **halts** fail-closed. Investigate.

## Resuming after a halt

V1 has no retry and no requeue. A `stopped` campaign is terminal: it
cannot be resumed. To proceed: diagnose the halt reason, fix the cause,
verify every host the campaign already touched (done, skipped and
orphaned rows), then create a new campaign for the remaining hosts if
appropriate. Never "resume" past a systemic halt by recreating the
same campaign unchanged.

## Rollback of one host (operator, via `.prev`)

No fleet auto-rollback exists. If a host installed N+1 but never
phones home:

1. Confirm on the host: `canonical -version` vs expected, and
   `.prev -version` (it holds the pre-upgrade binary).
2. Copy `.prev` back over the canonical path with mode 0755
   (operator action; keep a copy of the broken binary for analysis).
3. Ensure `/etc/cadence` identity is intact (it is never touched by
   upgrades; if it was damaged by other means, restore from backup).
4. Start the poll service (or wait for the next timer fire) and watch
   for authenticated polls at the restored version.
5. Only then consider the host eligible for a later campaign.

Never attempt a Campaigns downgrade: the agent refuses remote
downgrades unconditionally.

## Proof map (what proves what, no duplication)

- Crypto/install discipline (checksum, Minisign, ELF/arch,
  `-version`, atomic rename, 300 s dual-clock deadline): PR6 unit +
  subprocess tests.
- Version proof, proof timeout, backend restart safety: PR4 tests +
  PR8 unit E2E.
- Fleet progression, canary halt, gate, concurrency, pause/cancel,
  restart: C2 backend tests on real PostgreSQL.
- Real fleet rollout (timers, mTLS, rename, proof, `.prev` on two
  systemd hosts) + systemic blast-radius containment: C3 E2E
  (`sh scripts/test-agent-upgrade-e2e.sh`, phases 8-9).
- Proof-timeout disposition + real timeout composition: C2 (disposition
  `halt`) composed with PR8 (real 420 s timeout). C3 does not wait
  another 420 s to re-demonstrate it.

## CI status (infra follow-up, not this runbook's gate)

The E2E suite needs privileged Docker and ~35 minutes, so it stays out
of per-PR CI. A green manual run is required before merging fleet
changes. Automation target (separate infra task): a `workflow_dispatch`
+ nightly workflow on a dedicated privileged runner. Standard CI must
never depend on privileged Docker.
