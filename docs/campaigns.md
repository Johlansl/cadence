# Campaigns

A campaign rolls an `apt_upgrade` out across many hosts in ordered waves,
with a concurrency cap, a pause between waves to watch for fallout, and an
automatic stop when too much breaks. It is Cadence's answer to "upgrade the
fleet, but carefully": a canary wave first, then a bigger one, then the rest.

A campaign creates the same `apt_upgrade` jobs you would trigger by hand,
one per host, and the agent runs them unchanged. Everything campaign-specific
is server-side.

This guide is for an operator driving campaigns from the API or the
dashboard. For the internal design see
[architecture.md](architecture.md#campaigns) and
[decisions.md](decisions.md#campaigns).

All examples use invented ids and hostnames. Mutating calls need the
`X-Admin-Key` header; the `GET` views do not (same as the rest of the
dashboard API).

## The lifecycle

```
draft --activate--> running --pause--> paused --resume--> running
  |                    |                  |
  +------cancel--> cancelled <---cancel---+
                       |
        engine: every stage done      --> completed
        engine: a "halt" failure      --> stopped
        engine: skips > max_failures  --> stopped
```

A campaign is **created in `draft`**: its host set and per-host wave
assignment are fixed and stored, but nothing runs. You then **activate** it
in a separate call. This is deliberate: one create call can target the whole
fleet, so there is a review step before any job is queued.

`completed`, `stopped` and `cancelled` are terminal. The engine only ever
touches a `running` campaign.

## Create

Pick the hosts with **exactly one** of `host_ids` or `tag`. `tag` is matched
the same way as `GET /hosts?tag=` -- `"role=web"` for an exact pair, or a bare
`"web"` substring across keys and values. Whichever you use, the host set is
resolved **once, now**, and frozen; a host that changes tags later does not
join or leave the campaign.

`stages` is an ordered list of wave sizes. Each entry is:

- an **integer** -- that many hosts, absolute;
- **`"N%"`** -- floor of N percent of the resolved host count (N is 1..100);
- **`"rest"`** -- everything left; only valid as the last entry.

The sizes must cover every targeted host (end with `"rest"`, or make the
numbers add up). Hosts are ordered by hostname before slicing, so the waves
are stable.

```sh
curl -sS -X POST https://cadence.example/api/v1/admin/campaigns \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{
    "name": "March point release",
    "tag": "env=prod",
    "stages": [2, "25%", "rest"],
    "max_concurrency": 5,
    "max_failures": 3,
    "observation_window_seconds": 900
  }'
```

| field | meaning |
|---|---|
| `name` | free text, for the dashboard and the audit log |
| `host_ids` \| `tag` | the target set, resolved once (give exactly one) |
| `stages` | ordered wave sizes (see above) |
| `max_concurrency` | most `apt_upgrade` jobs in flight for this campaign at once, across all waves (>= 1) |
| `max_failures` | how many hosts may be *skipped* before the campaign stops; the campaign stops when the skip count **exceeds** this, so `0` means "stop on the first skip" |
| `observation_window_seconds` | quiet time after a wave's last job finishes before the next wave starts (also before `completed`). Optional; defaults to `CADENCE_CAMPAIGN_OBSERVATION_WINDOW_SECONDS` (600). `0` advances immediately |

The response is the full campaign, including a `stages_detail` rollup and the
per-host list. It is in `draft`.

## Activate, and the manual controls

```sh
curl -sS -X POST .../api/v1/admin/campaigns/$ID/activate -H "X-Admin-Key: $KEY"
```

`activate` (`draft` -> `running`) is the only way a campaign starts. The
engine picks it up on its next tick (within ~60 s).

- **`pause`** (`running` -> `paused`) -- the engine stops creating new jobs
  and stops reconciling results; jobs already running on their hosts finish
  normally. `resume` (`paused` -> `running`) catches up on everything that
  happened while paused, in one pass.
- **`cancel`** (`draft` / `running` / `paused` -> `cancelled`) -- no more
  jobs are created. Jobs already in flight keep running on their agents (there
  is no remote job-kill); each such host is marked `orphaned` and its real
  result still lands in the job record, it just no longer counts toward the
  campaign.

Any illegal transition is a `409`.

## What the engine does each tick

For every `running` campaign, once per scheduler pass:

1. **Reconcile.** Every host whose job finished:
   - `succeeded` plus health `healthy` or `degraded`, and the agent did
     not reboot on the job -> `done`;
   - `succeeded` with `will_reboot=true` -> stays `running`, awaiting
     proven return (see "Reboots" below); `done` only once the host is
     back on a new boot with fresh acceptable health;
   - `succeeded` plus `unhealthy` -> stop the campaign immediately with
     `health_unhealthy`;
   - `succeeded` plus missing, `unknown` or unrecognized health -> stop the
     campaign immediately with `health_unknown`;
   - `failed` -> a **disposition** decided by the job's `failure_category`:

     | `failure_category` | disposition |
     |---|---|
     | `apt_locked`, `dpkg_error`, `disk_full`, `timeout`, `agent_lost`, `agent_refused` | **skip** |
     | `network_or_repo`, `unknown`, anything unmapped / missing | **halt** |

     A **skip** drops that host from the rest of the campaign and counts
     toward `max_failures`; the wave carries on for the others. A **halt**
     stops the whole campaign immediately, on the first occurrence.
2. **Stop check.** Halt disposition, or skip count past `max_failures` ->
   `status = 'stopped'`, `halt_reason` recorded. Any host still in flight is
   marked `orphaned` (see `cancel` above); a host whose reboot was never
   proven is orphaned too, never `done`.
3. **Fill.** Create jobs for the current wave's not-yet-started hosts, up to
   `max_concurrency` (counted over this campaign's `running` rows, reboot
   awaits included). A host that already has an unrelated job pending or
   running is left for the next tick, not failed.
4. **Gate.** A wave whose jobs have all finished still **waits the
   observation window** (measured from its last job's completion) before the
   next wave starts. When there is no next wave, the campaign is `completed`.

The disposition table lives in code (`app/campaigns/engine.py`), not the
database, so it can be tuned without a migration.

The health gate requires agent `0.12.0` or newer. An older agent's successful
job has no `health_status`, so the campaign stops with `health_unknown` rather
than advancing without evidence. Upgrade every target agent before activation.

## Reboots

An `apt_upgrade` that needs a reboot reboots when the host's `reboot_policy`
is `auto` (a per-job `reboot` override wins; `never` and `prompt` never
reboot from a campaign job). A rebooting host is not `done` when its job
succeeds: the engine holds its row at `running` until the host **proves its
return**, then the wave carries on. The lifecycle is:

```
upgrade succeeds, will_reboot=true
  -> running, pre-reboot boot snapshotted (awaiting return)
  -> host reboots, agent checks in from a new boot
  -> new boot_id + fresh healthy/degraded health
  -> done, wave progresses
```

**Proof is the kernel boot id.** Every agent contact (reports, polls, job
results, the boot health-check claim) carries `boot_id`, and the server
remembers the latest per host. A job result also carries `will_reboot`,
the agent's own reboot decision: an explicit `false` means no reboot
follows (the host completes immediately), `true` starts the wait above,
and a missing key means an older agent (see fail-closed below). Freshness
is server-side: the host's `health_checked_at` must postdate the moment
the wait started, so a stale pre-reboot verdict can never satisfy it.

**Two caps apply.** `max_concurrency` is per campaign and counts `running`
rows, awaits included, so a rebooting host keeps its wave slot until it
is proven back (or the campaign stops). The **global reboot budget**
(`CADENCE_MAX_CONCURRENT_REBOOTS`, default `0` = disabled) caps
Cadence-driven reboots awaiting proof across campaigns, schedules and
manual jobs: creating a job that may reboot past the cap is refused
(`409` for admin calls; the scheduler and the engine retry on the next
tick).

**Fail-closed.** A reboot the server cannot prove stops the campaign
rather than guessing:

- an `auto` upgrade that required a reboot but sent no proof fields
  (older agent) halts immediately with `boot_proof_missing`;
- a `will_reboot=true` result with no `boot_id` halts the same way;
- an await still unproven after `CADENCE_CAMPAIGN_RETURN_TIMEOUT_SECONDS`
  (default 1800) halts with `return_timeout`.

**Holds are never released implicitly.** A host whose reboot is unproven
keeps consuming the global budget past a return timeout, a stop, a pause
or a cancel, and the retention sweep will not delete a job that still
carries such a hold. Only two things release it: the host observed back
on a new boot, or an explicit operator recovery
(`DELETE /api/v1/admin/hosts/{id}/jobs`, which clears the host's job
history). Plan for the recovery path before enabling the budget on a
fleet with old-agent history: a legacy `auto` upgrade in the past holds
a slot until cleared.

**Rollout preconditions.** Before running a campaign over rebooting
hosts: every target runs an agent that sends `boot_id` / `will_reboot`;
`cadence-agent-health-check-boot.timer` is enabled so a fresh health
verdict lands right after boot (fresh health is part of the proof); and
the budget is set above `0` if you want the fleet-wide bound (at `0` the
per-campaign await still applies, only the global cap is off).

## Read status

```sh
curl -sS .../api/v1/campaigns            # list, newest first
curl -sS .../api/v1/campaigns/$ID        # one campaign, with detail
```

The detail response has:

- the campaign fields, plus recomputed `hosts_total` / `hosts_done` /
  `hosts_skipped` / `hosts_orphaned` and `current_stage_index`;
- `stages_detail` -- per wave: `size_spec`, `hosts_total`, and the
  `pending` / `running` / `done` / `skipped` / `orphaned` breakdown;
- `hosts` -- per host: `hostname`, `stage_index`, `state`, `skip_reason`
  (the `failure_category` when skipped), and `job_id` once a job exists.

Per-host `state` is `pending` -> `running` -> `done` | `skipped`, or
`orphaned` if a stop / cancel caught its job mid-flight. There is no
separate "awaiting" state: a host rebooting reads `running` until its
return is proven.

## Webhooks

If you have a webhook configured (see [webhooks.md](webhooks.md)), subscribe
it to any of:

| event | when | `data` beyond `campaign_id`, `name` |
|---|---|---|
| `campaign.stage_completed` | a wave's last host reaches a terminal state | `stage_index`, `hosts_total`, `hosts_done`, `hosts_skipped`, `hosts_orphaned` |
| `campaign.completed` | the last wave's window elapsed, nothing halted | `status`, `hosts_*` totals |
| `campaign.stopped` | a halt disposition, or skips past `max_failures` | `status`, `reason`, `halt_category`, `halt_host` (the latter two null for a `max_failures` stop), `hosts_*` totals |

There is no `campaign.activated` event: you just made that call.

## Notes and limits

- A campaign reboots exactly like a hand-triggered upgrade: the hosts'
  `reboot_policy` (or a per-job `reboot` override) decides, and every reboot
  is awaited and proven as described in "Reboots" above.
- `params.excluded_packages` / `params.known_held_packages` are injected into
  every campaign job exactly as for a hand-triggered `apt_upgrade`, so your
  exclusion rules apply unchanged. `params.reboot` is also snapshotted at
  creation, so a later policy change does not rewrite a job's meaning.
- The retention sweep will not delete a finished job while its campaign is
  still `draft` / `running` / `paused`, and never deletes a job still
  holding an unproven reboot (see "Reboots" above).
- A campaign job remains an ordinary `apt_upgrade`, but the health gate now
  requires agent `0.12.0` or newer on every target.
