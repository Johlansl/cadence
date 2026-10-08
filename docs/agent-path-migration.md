# Agent path migration runbook (`/usr/local/bin` to `/usr/bin`)

No host may receive a real `agent_upgrade` job until it is proven to run
the agent from the canonical path `/usr/bin/cadence-agent` through its
**effective** systemd configuration. This runbook migrates the historical
fleet to that state. It is an explicit operator procedure: Cadence never
orchestrates it over SSH, and no fleet engine is involved (PR7 adds none).

Companion script (read-only, run on each host):

- `scripts/check-agent-host-upgrade-readiness.sh` -- exit 0 means 6B-ready,
  anything else lists what to fix. No writes, no network, never prints
  secrets. Fixture-tested by `scripts/test-agent-readiness.sh` (29 checks).

## 1. Historical states

| State | Binary | Units | Effective ExecStart |
|---|---|---|---|
| A. old install.sh | `/usr/local/bin/cadence-agent` | `/etc/systemd/system` (old) | `/usr/local/bin/...` |
| B. install.sh PR1+ | `/usr/bin/cadence-agent` | `/etc/systemd/system` (current) | `/usr/bin/...` |
| C. pure .deb | `/usr/bin/cadence-agent` | `/usr/lib/systemd/system` (from the package) | `/usr/bin/...` |
| D. mixed history | `/usr/bin/...` (recent .deb) | stale `/etc` units masking `/usr/lib` | often still `/usr/local/bin/...` |
| E. custom admin | unknown | overrides / drop-ins | must be read, never assumed |

A source unit file proves nothing by itself: `/etc` masks `/usr/lib`,
and drop-ins override both. Only the loaded configuration counts:

```sh
systemctl show -p LoadState,FragmentPath,DropInPaths,ExecStart -- cadence-agent.service
systemctl show -p LoadState,FragmentPath,DropInPaths,ExecStart -- cadence-agent-poll.service
systemctl show -p LoadState,FragmentPath,DropInPaths,ExecStart -- cadence-agent-health-check-boot.service
```

**6B-ready = every service above is `loaded`, its effective `ExecStart`
runs exactly `/usr/bin/cadence-agent`, it carries no drop-in, the three
timers are loaded and enabled, the canonical binary exists (regular file,
executable, mode 0755, `-version` prints N.N.N), and the host is enrolled
(`/etc/cadence/agent.env` present).** The readiness script checks exactly
this. Absence of `/usr/local/bin/cadence-agent` alone is NOT proof.

## 2. Before touching anything (every host)

1. Copy the readiness script to the host (or run it from a checkout) and
   capture the baseline:
   `sh check-agent-host-upgrade-readiness.sh | tee /root/6b-before.txt`
2. Record identity fingerprints (hashes only, never contents):
   ```sh
   sha256sum /etc/cadence/agent.env /etc/cadence/* 2>/dev/null | tee /root/6b-identity-before.txt
   stat -c '%n %a %u %g' /etc/cadence/* | tee -a /root/6b-identity-before.txt
   ```
3. Note which state (A-E) the report indicates.

What changes during migration: the binary and the unit files only.
What must never change: `/etc/cadence/agent.env`, client cert/key,
HMAC token, server CA, host identity. Re-run the fingerprint step after
migration and diff: any difference outside the intended files stops the
rollout.

## 3. Procedures per category

### New host (recent install)

1. Run the readiness script; expect `6B-READY`.
2. Done. No migration.

### install.sh history (state A)

Prefer the enrolled upgrade path of the curl/sh installer, which is
exactly built for this: it requires an enrolled agent, refreshes the
binary plus the `/etc` units to the canonical path, preserves the whole
credential bundle (no new enrollment code), reloads systemd, enables the
timers, and fires one report:

```sh
CADENCE_BASE_URL=https://<site> CADENCE_VERIFIED_CA=<pinned-ca> \
  CADENCE_UPGRADE_ONLY=true sh agent-install.sh
```

From a repo checkout instead, re-running `agent/systemd/install.sh`
(needs a freshly built PR6 binary as argument) does the same for binary
plus units and never overwrites an existing `agent.env`, followed by
`daemon-reload` and the timer enables it performs itself.

Then: section 4 (validate), section 2 step 2 again (diff identities).

### Pure .deb (state C)

1. Run the readiness script; expect `6B-READY`.
2. No mutation. Never touch `/usr/lib` by hand; future `.deb` upgrades
   own those files.

### Mixed history (state D)

Symptom: the binary is already `/usr/bin` but at least one effective
`ExecStart` still names `/usr/local/bin`, with `FragmentPath` under
`/etc/systemd/system` masking the packaged `/usr/lib` unit.

Two repairs, either is fine; do not mix them on one host:

- (a) Converge to state B: rerun one installer as in "install.sh
  history". The `/etc` units are rewritten with the canonical path and
  masking stops mattering. Simplest and uniform.
- (b) Stay pure .deb (state C): remove ONLY the stale masking units
  after confirming each is byte-comparable to a historical shipped unit
  and carries no admin customization, then `daemon-reload`:
  ```sh
  rm /etc/systemd/system/cadence-agent.service /etc/systemd/system/cadence-agent-poll.service ...
  systemctl daemon-reload
  systemctl cat cadence-agent.service   # must now show the /usr/lib fragment
  ```
  If unsure about any file, use (a) instead.

Then: section 4.

### Custom overrides (state E)

Fail closed. If `DropInPaths` is non-empty or a unit was hand-edited for
a local reason (proxy env, hardening, paths), do NOT let any installer
overwrite or delete it blindly:

1. Read every drop-in (`systemctl cat <service>` shows the full stack).
2. Understand why it exists; carry the intent forward by hand.
3. Only then rerun the installer (which replaces base units but keeps
   drop-ins), re-check effective `ExecStart`, and remove the drop-in
   only when it is proven redundant.
4. The readiness script keeps failing until no unknown override
   remains. That is the point.

## 4. Validate after migration (every host)

1. `sh check-agent-host-upgrade-readiness.sh` -- must print `6B-READY`.
2. Fire a real oneshot and watch it authenticate:
   ```sh
   systemctl start cadence-agent.service
   journalctl -u cadence-agent -n 20 --no-pager
   ```
3. On the server, confirm the host reported (fresh `last_seen_at`,
   `agent_version` still flowing). No re-enrollment must have happened.
4. Re-run the identity fingerprints from section 2 and diff against
   before: identical.
5. No host reboot is required. The oneshot timers pick up the new inode
   on their next fire.

## 5. Rollback (migration only, not PR6 rollback)

If the rewritten units fail to start, the old state is recoverable
because nothing below deletes credentials and the old binary is kept
until validation (section 6):

1. `journalctl -u cadence-agent* --no-pager` -- read the actual error.
2. If a unit file is at fault, restore it from the pre-migration copy
   the operator saved in section 2 (or from the previous installer),
   `daemon-reload`, `systemctl start cadence-agent.service`.
3. If the new binary fails, the previous binary is still the one the
   old units point at: restoring the old units restores the old binary.
4. Re-run the readiness script to confirm the host is back to its
   documented pre-migration state, then diagnose before retrying.

Rollback stays manual. There is deliberately no automation here.

## 6. Removing `/usr/local/bin/cadence-agent`

Only when ALL hold:

- readiness script prints `6B-READY`;
- its residue line says the legacy binary is **unreferenced**
  (no effective `ExecStart`, no drop-in, no active path names it);
- post-migration validation (section 4) passed, including one
  authenticated report from `/usr/bin`.

Then, exactly:

```sh
rm /usr/local/bin/cadence-agent
```

If the script reports the residue as still referenced, STOP: deleting
it would break the running agent. Migrate the units first. Never
`rm -f` blindly.

## 7. Canary progression (mandatory)

There is no waves engine; there is a mandatory order:

1. Pick one non-critical host; capture before-state (section 2).
2. Migrate it (section 3).
3. Validate it (section 4) plus identity diff.
4. Only when the canary has reported cleanly for at least one full
   timer cycle (hourly report + minute polls), repeat on the next
   small batch, then the rest.
5. Track per host: before-report, procedure used, after-report,
   identity diff result. A host without an after-report is not done.

## 8. Fleet qualification

Approach: local diagnostic per host plus the operator checklist above.
No protocol change, no new agent field, no dashboard in PR7.

A server-visible executable path in reports was considered and
rejected for this lot: it would need an agent change plus a backend
column/migration to observe a one-shot migration, while safety does
not depend on it -- PR6 already refuses to upgrade unless its own
resolved executable is exactly `/usr/bin/cadence-agent`. Central
visibility is convenient, not required for safety. Revisit only if
the fleet proves too large for the checklist.

## 9. Checklist: host cleared for its first real `agent_upgrade`

Every box required, in this order:

- [ ] PR6-capable agent release installed manually (fleet cannot
      self-upgrade to the first 6B release).
- [ ] Readiness script prints `6B-READY` (effective units canonical,
      no unknown override, timers enabled, enrolled).
- [ ] One authenticated poll/report observed after migration.
- [ ] `hosts.agent_version` flowing and correct for the host.
- [ ] Credential fingerprints identical before/after.
- [ ] PR2 artifacts for the target version published, immutable,
      signed, both architectures.
- [ ] Server proof timeout safely configured (non-zero recommended;
      never `proof=0` together with generic also `0` -- that removes
      the fail-safe recovery of a silent job even though it is
      race-safe).
- [ ] `.prev` recovery documented to whoever is on call (run
      `/usr/bin/cadence-agent.prev -version`, or reinstall; operator
      action, no auto-rollback).

## 10. What this lot explicitly does NOT do

No server-driven SSH, no Campaigns/rollout/waves/canary engine, no
fleet retry, no rollout UI, no remote generic command, no arbitrary
shell from the server, no new root primitive, no auto-rollback, no new
state machine. The initial migration stays out-of-band precisely
because old agents are not yet a homogeneous safe base.
