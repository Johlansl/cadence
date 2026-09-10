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
   - `succeeded` plus health `healthy` or `degraded` -> `done`;
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
   marked `orphaned` (see `cancel` above).
3. **Fill.** Create jobs for the current wave's not-yet-started hosts, up to
   `max_concurrency` (counted over this campaign's `pending` + `running`
   jobs). A host that already has an unrelated job pending or running is left
   for the next tick, not failed.
4. **Gate.** A wave whose jobs have all finished still **waits the
   observation window** (measured from its last job's completion) before the
   next wave starts. When there is no next wave, the campaign is `completed`.

The disposition table lives in code (`app/campaigns/engine.py`), not the
database, so it can be tuned without a migration.

The health gate requires agent `0.12.0` or newer. An older agent's successful
job has no `health_status`, so the campaign stops with `health_unknown` rather
than advancing without evidence. Upgrade every target agent before activation.

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
`orphaned` if a stop / cancel caught its job mid-flight.

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

- A campaign never reboots. Set the hosts' `reboot_policy`, or run a reboot
  campaign-style rollout separately; sequenced reboots are not a campaign
  feature.
- `params.excluded_packages` / `params.known_held_packages` are injected into
  every campaign job exactly as for a hand-triggered `apt_upgrade`, so your
  exclusion rules apply unchanged.
- The retention sweep will not delete a finished job while its campaign is
  still `draft` / `running` / `paused`.
- A campaign job remains an ordinary `apt_upgrade`, but the health gate now
  requires agent `0.12.0` or newer on every target.
