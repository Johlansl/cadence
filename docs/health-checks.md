# Upgrade health checks

Every `apt_upgrade` run performed by agent `0.12.0` or newer surrounds the apt
action with structured checks. They answer two different questions:

- did the requested upgrade action complete (`jobs.status`)?
- what state was the host in after that attempt (`health_status`)?

These answers are deliberately independent. A successful apt exit does not
hide a newly failed service, and a failed apt action does not discard useful
post-check evidence.

## Before the upgrade

Checks run in this order:

| Check | Probe | Blocking result |
|---|---|---|
| Disk space | available bytes on each distinct filesystem backing `/var`, `/boot`, `/boot/efi`; missing boot paths are ignored | below threshold or measurement unavailable |
| Package-manager locks | POSIX advisory locks used by apt and dpkg, polled until the configured deadline | still held at the deadline or inspection unavailable |
| dpkg audit | `dpkg --audit` | any reported problem or command failure |
| APT dependencies | `apt-get check` | command failure |
| Failed services baseline | `systemctl --failed --no-legend --plain --no-pager` | inspection unavailable; existing failures are only a warning |
| Package indexes | `apt-get --error-on=any update` | refresh failure; an old apt without strict-mode support falls back to `apt-get update` and records a warning |

A `failed` or `unknown` blocking check prevents `apt-get dist-upgrade` from
starting. Remaining checks are recorded as `skipped`. The failed job uses the
existing failure categories (`disk_full`, `apt_locked`, `dpkg_error`,
`network_or_repo`, `timeout`, or `agent_refused`) so existing webhook consumers
and campaign failure policy keep one vocabulary.

The health-check layer does not run `apt --fix-broken install`, restart
services, or free disk space. It reports evidence and leaves the operator in
control. The existing failed-upgrade `dpkg --configure -a` recovery remains
part of the action path, not a check.

## After the upgrade attempt

Once the pre-check phase passes, these checks run after the action path whether
the action succeeds or fails:

1. `dpkg --audit`;
2. `apt-get check`;
3. the same disk-space measurements;
4. failed systemd services compared with the pre-upgrade baseline;
5. reboot-required detection.

A newly failed service is `failed`; a service already failed before the
upgrade is `warning`. A required reboot is also `warning`, not a failed
upgrade. The phase aggregate maps to host health:

| Post-check phase | Host health | Campaign behavior after an action success |
|---|---|---|
| `passed` | `healthy` | continue |
| `warning` | `degraded` | continue |
| `failed` | `unhealthy` | stop with `health_unhealthy` |
| `unknown` | `unknown` | stop with `health_unknown` |

The host row stores only the latest projection (`health_status` and
`health_checked_at`). Full ordered evidence remains in the job's
`result.pre_checks` and `result.post_checks` and is visible in the collapsible
health-check detail on the host page. The migration initializes existing hosts
to `unknown` and does not invent historical evidence.

## Thresholds

The server injects its current thresholds into every newly created
`apt_upgrade` job, whether it came from the dashboard, a maintenance window or
a campaign. This preserves the policy used for that run in `jobs.params`.

| Server variable | Default |
|---|---|
| `CADENCE_UPGRADE_MINIMUM_AVAILABLE_BYTES` | 1 GiB for `/var` |
| `CADENCE_UPGRADE_BOOT_MINIMUM_AVAILABLE_BYTES` | 200 MiB for `/boot` and `/boot/efi` |
| `CADENCE_UPGRADE_LOCK_WAIT_SECONDS` | 120 seconds (maximum 3600) |

An agent receiving a job without these settings, for example from an older
server, uses the same defaults locally.

## Triggers outside of apt_upgrade

Besides an `apt_upgrade`'s own post-checks, exactly two other things refresh
`health_status`, both via a standalone `health_check` job (agent `0.13.0`+)
that reruns the same post-check probes with no upgrade attached:

- **Manual**: a "health check" button on the host page, next to "dry run" and
  "trigger dist-upgrade", same pattern.
- **Automatic, once per boot**: a systemd unit
  (`cadence-agent-health-check-boot.timer`) fires about 45 seconds after
  every boot, whatever caused it -- a Cadence `apt_upgrade` with
  `reboot: auto`, a manual SSH reboot, a Proxmox-level reboot. It asks a new
  `POST /api/v1/agent/health-check-job` to atomically create and claim a
  `health_check` job for the calling host, then runs it exactly like any
  other job. The trigger is the boot itself (systemd), not agent-persisted
  state: the agent stays stateless between runs.

A plain periodic report never touches `health_status`: this is deliberate,
ruled out for its continuous, unbounded cost. These three triggers --
`apt_upgrade`'s own post-checks, the manual button, the once-per-boot check
-- are the only ones.

A `health_check` result carries `post_checks` and `health_status` but never
`pre_checks` (there is no pre-check phase without an upgrade to gate). Its
failed-services check never distinguishes "new" from "pre-existing": a
currently-failed service is always a `warning`, since a standalone check has
no pre-run baseline in the same job to diff a "new" failure against.

## Compatibility and rollout

The server continues to accept job results from older agents. Their jobs have
no structured checks and do not update the host health projection, which
therefore stays at its previous value or `unknown`.

Campaigns are intentionally stricter: a successful campaign job without a
recognized health result stops the campaign with `health_unknown`. Upgrade the
target hosts to agent `0.12.0` or newer before starting a campaign after this
server change. Single-host and scheduled jobs remain backward compatible.

Existing `job.succeeded` and `job.failed` webhook events gain
`health_status`, `pre_checks` and `post_checks`; the fields are `null` when
not applicable. No new webhook event type is introduced.
