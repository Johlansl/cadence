# Webhooks

Cadence can POST a small JSON body to an HTTP endpoint you control when
something happens in your fleet: a job finishes, a host goes quiet, a host
starts needing a reboot, or a host gains pending security updates. Every
delivery is signed. The body is one stable shape for every event and every
receiver: Cadence does no per-recipient formatting.

This guide is for an operator wiring Cadence up to their own tooling. For the
internal design (why an outbox, why the dispatcher runs on the scheduler tick)
see [architecture.md](architecture.md#webhooks); for the security properties
see [SECURITY.md](../SECURITY.md).

All examples below use invented ids, hostnames and log text.

## Heads up: this is a generic JSON feed, not a chat message

Cadence sends its own envelope (`event_type`, `timestamp`, `delivery_id`,
`data`). It is **not** the body that Discord, Slack, Microsoft Teams or similar
chat tools expect, and Cadence does not translate to those formats, by design:
one signed contract keeps every consumer (a relay, an automation platform, a
log sink, your own service) on the same shape.

If you point a webhook straight at a chat "incoming webhook" URL, every
delivery fails:

- **Discord** rejects a body with no `content` / `embeds` / file:
  `400` `{"code": 50006, "message": "Cannot send an empty message"}`.
- **Slack** wants `{"text": "..."}` or Block Kit `blocks`; anything else is a
  `400` (`invalid_payload` / `no_text`).
- **Teams** wants an Adaptive Card delivered through a Workflows / Power
  Automate flow, or a MessageCard on the legacy connector.

A failed delivery is retried and then parked as `failed` (see
[Delivery behaviour](#delivery-behaviour)). To reach a chat tool, put a small
relay in between, see [Sending to Discord, Slack or
Teams](#sending-to-discord-slack-or-teams).

## Enabling and configuring

The dashboard has a **Webhooks** section (top nav). Add a webhook there: an
`https://` URL, a description, and the events to subscribe to. The full URL and
the signing **secret are shown once, at creation** and never again; copy the
secret then. Editing, disabling and a "send test" button are on each row.

The same thing over the admin API:

```sh
# On the server, straight to the backend (skips Caddy's dashboard basic-auth):
curl -X POST http://127.0.0.1:8000/api/v1/admin/webhooks \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{
        "url": "https://relay.example.com/cadence",
        "event_types": ["job.failed", "host.offline"],
        "description": "chat relay"
      }'
```

`/api/v1/admin/*` sits behind the dashboard basic-auth gate as well as
`X-Admin-Key`. From off the server, add `-u "$CADENCE_DASHBOARD_USER:<password>"`
and use the public `https://<site>` URL; on the server, calling
`http://127.0.0.1:8000` directly is simpler.

The `GET /api/v1/webhooks` list is a normal dashboard read (no `X-Admin-Key`),
but it never returns the raw URL or the secret. The URL comes back masked: the
scheme, host and first few path segments are kept, the rest becomes a bullet,
so `https://host/api/webhooks/123456/aB9x...` shows as
`https://host/api/webhooks/123456/•••`.

Tuning knobs are the `CADENCE_WEBHOOK*` variables in
[README.md](../README.md#server-env). The feature is inert until at least one
webhook exists; `CADENCE_WEBHOOKS_ENABLED=false` turns it off entirely.

## What Cadence sends

### Envelope

Every POST body is:

```json
{
  "event_type": "job.failed",
  "timestamp": "2026-01-15T09:42:11.503817Z",
  "delivery_id": "6b1f5e2a-9c7d-4e11-8a3b-2f9d4c7e1a08",
  "data": {}
}
```

| Field | Meaning |
|---|---|
| `event_type` | one of the events below (or `webhook.test`) |
| `timestamp` | when the event occurred, ISO 8601, UTC, `Z` suffix, microseconds. Does **not** change between retries |
| `delivery_id` | UUID of this delivery. Stable across retries. Use it to deduplicate: delivery is at-least-once |
| `data` | event-specific, see below |

### Headers

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `User-Agent` | `cadence-webhook` |
| `X-Cadence-Event` | the `event_type` |
| `X-Cadence-Delivery` | the `delivery_id` (same as in the body) |
| `X-Cadence-Timestamp` | Unix seconds, the **send time of this attempt**. Changes on every retry |
| `X-Cadence-Signature` | HMAC-SHA256, hex. See [Verifying the signature](#verifying-the-signature) |

### `job.succeeded` / `job.failed`

Fires when an agent reports a job result, for any job type (`apt_upgrade`,
`reboot`, `apt_dry_run`, ...). `job.failed` also fires when the scheduler
gives up on a job that was claimed but never reported back (see `reaped`
below). A dry-run (`job_type: "apt_dry_run"`) is a pure-read simulation and
still emits `job.succeeded` / `job.failed`; filter on `job_type` if you only
want real upgrades. The structured preview lives on the job record
(`result.dry_run`), not in the webhook body.

```json
{
  "event_type": "job.failed",
  "timestamp": "2026-01-15T09:42:11.503817Z",
  "delivery_id": "6b1f5e2a-9c7d-4e11-8a3b-2f9d4c7e1a08",
  "data": {
    "job_id": "b7e41d02-2c9a-4f77-9d3e-1a5c8e0b2f44",
    "host_id": "8e543e0c-6b2a-4d1f-9c7e-3a2b1d0f4e56",
    "hostname": "web-01",
    "job_type": "apt_upgrade",
    "status": "failed",
    "exit_code": 100,
    "reaped": false,
    "failure_category": "dpkg_error",
    "failure_summary": "E: Sub-process /usr/bin/dpkg returned an error code (1)",
    "requested_by": "scheduler",
    "completed_at": "2026-01-15T09:42:11.480902Z",
    "log": "Reading package lists...\nBuilding dependency tree...\n\n[cadence] ... 5123 bytes of log elided ...\n\nE: Sub-process /usr/bin/dpkg returned an error code (1)\n"
  }
}
```

- `status` is `"succeeded"` or `"failed"` (matches `event_type`).
- `exit_code` is the command's exit code (`0` on success), or `null` for a
  reaped job (no process ran to completion).
- `requested_by` is `"scheduler"`, `"dashboard"`, or `null`.
- `completed_at` may be `null`.
- `log` is truncated to `CADENCE_WEBHOOK_LOG_MAX_BYTES` (4096 by default): the
  head and tail are kept and the middle is replaced by a
  `[cadence] ... N bytes of log elided ...` marker. Set the variable to `0` to
  send the whole log.

**`job.failed` only** (absent from `job.succeeded`):

- `reaped` is `true` when the scheduler failed the job because it stayed
  `running` past `CADENCE_JOB_RUNNING_TIMEOUT_SECONDS` with no result, `false`
  when the agent reported the failure itself.
- `failure_category` is a coarse cause, one of:

  | value | meaning |
  |---|---|
  | `apt_locked` | the apt/dpkg lock was still held after the agent's own retries |
  | `network_or_repo` | a mirror was unreachable or a download failed |
  | `dpkg_error` | a dpkg processing error, a broken package state, or a dependency / file conflict |
  | `disk_full` | out of disk space |
  | `timeout` | the run exceeded its time limit (agent-side deadline, or reaped while the host was still reporting) |
  | `agent_lost` | reaped with no result while the host was silent |
  | `agent_refused` | the agent declined the job before running anything (upgrades or reboots disabled on the host, unsupported job type) |
  | `unknown` | nothing matched; read `failure_summary` and `log` |

  It may be `null` for a job failed by an agent older than 0.9.0. The set is
  open: treat an unrecognised value like `unknown`.
- `failure_summary` is one line pulled from the apt/dpkg output (or a synthetic
  line for the no-output cases), so a relay need not parse `log`. May be
  `null`.

### `host.reboot_required`

Fires once, when a report flips a host from not needing a reboot to needing
one. It does not re-fire while the host stays in that state.

```json
{
  "event_type": "host.reboot_required",
  "timestamp": "2026-01-15T06:15:44.221060Z",
  "delivery_id": "1d0a7c33-4b8e-4f2a-9c11-77e5a2b6d904",
  "data": {
    "host_id": "8e543e0c-6b2a-4d1f-9c7e-3a2b1d0f4e56",
    "hostname": "web-01",
    "reboot_required": true
  }
}
```

### `host.security_updates_available`

Fires when a report's count of pending security updates is above zero **and**
differs from the count at the last notification sent for that host.

```json
{
  "event_type": "host.security_updates_available",
  "timestamp": "2026-01-15T06:15:44.221060Z",
  "delivery_id": "9f3c1e77-2a5d-4b0c-8e6f-1b2c3d4e5f60",
  "data": {
    "host_id": "b1c2d3e4-f5a6-4718-9a0b-1c2d3e4f5a6b",
    "hostname": "db-02",
    "security_updates_count": 3,
    "previous_count": null
  }
}
```

`previous_count` is `null` the first time, then the count reported at the
previous notification (so 5 -> 0 -> 3 fires again at 3, but 3 -> 3 does not).

### `host.offline`

Fires once when a host's `last_seen_at` gets older than
`CADENCE_WEBHOOK_OFFLINE_AFTER_SECONDS` (900 by default). It re-arms when the
host reports again; there is no matching "back online" event.

```json
{
  "event_type": "host.offline",
  "timestamp": "2026-01-15T12:03:00.117402Z",
  "delivery_id": "4e5f6a7b-8c9d-4e0f-1a2b-3c4d5e6f7a80",
  "data": {
    "host_id": "8e543e0c-6b2a-4d1f-9c7e-3a2b1d0f4e56",
    "hostname": "web-01",
    "last_seen_at": "2026-01-15T11:42:07.003920Z",
    "threshold_seconds": 900
  }
}
```

### `webhook.test`

Not subscribable. This is what the dashboard "send test" button (and
`POST /api/v1/admin/webhooks/{id}/test`) produces. It goes through the exact
same signed, retrying delivery path, so it is the way to check a receiver or a
relay before real events start flowing. It is delivered even to a disabled
webhook.

```json
{
  "event_type": "webhook.test",
  "timestamp": "2026-01-15T14:00:02.884019Z",
  "delivery_id": "0f2c9a11-3b4c-4d5e-6f70-8192a3b4c5d6",
  "data": {
    "message": "Test delivery from Cadence.",
    "webhook_id": "7a6b5c4d-3e2f-4102-8f3e-4d5c6b7a8901"
  }
}
```

## Delivery behaviour

- **Outbox.** The event is written to the database in the same transaction as
  the change that caused it, so it is never lost, even if Cadence restarts
  before it is sent.
- **First attempt** happens on the next scheduler pass, within about 60
  seconds.
- **Retries.** A non-2xx response, a timeout
  (`CADENCE_WEBHOOK_TIMEOUT_SECONDS`, 10s), or a connection error is retried
  with exponential backoff: roughly 60s, 120s, 240s, 480s, 960s between
  attempts, capped at 1 hour, up to `CADENCE_WEBHOOK_MAX_ATTEMPTS` (6). After
  that the delivery is `failed` and not retried.
- **At-least-once.** A slow receiver that eventually 200s after Cadence already
  timed out will see the delivery again. Deduplicate on `delivery_id`.
- **Ordering is not guaranteed.** Sort by `timestamp` if you need it.
- **State** per webhook is on its dashboard row: last successful delivery time,
  pending count, failed count, and the last error string. Old `delivered` /
  `failed` rows are pruned after `CADENCE_WEBHOOK_DELIVERIES_RETENTION_DAYS`
  (30).
- Redirects are **not** supported; a webhook URL that answers with a 3xx is a
  misconfiguration.

## Verifying the signature

`X-Cadence-Signature` is `HMAC-SHA256`, keyed with the webhook's secret, over
this exact string:

```
<X-Cadence-Timestamp> + "\n" + sha256_hex(<raw request body bytes>)
```

that is, the send timestamp, a single newline, and the lowercase hex SHA-256 of
the body **exactly as received**. Cadence serializes the body as compact,
key-sorted JSON; do not re-serialize it before hashing, hash the raw bytes.

Because `X-Cadence-Timestamp` is inside the signed string, a captured request
replayed with a fresh timestamp fails the HMAC, and one replayed verbatim fails
a freshness check. Reject anything older (or newer) than a few minutes; 300
seconds matches Cadence's own agent auth window.

### Python (Flask)

```python
import hashlib
import hmac
import os
import time

from flask import Flask, abort, request

app = Flask(__name__)
SECRET = os.environ["CADENCE_WEBHOOK_SECRET"].encode()  # shown once at creation
TOLERANCE_SECONDS = 300


@app.post("/cadence-webhook")
def receive():
    raw = request.get_data()  # raw bytes, exactly as received
    ts = request.headers.get("X-Cadence-Timestamp", "")
    sig = request.headers.get("X-Cadence-Signature", "")

    if not ts.isdigit() or abs(time.time() - int(ts)) > TOLERANCE_SECONDS:
        abort(401, "stale or missing timestamp")

    canonical = f"{ts}\n{hashlib.sha256(raw).hexdigest()}".encode()
    expected = hmac.new(SECRET, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        abort(401, "bad signature")

    event = request.get_json()
    app.logger.info("%s %s %s", event["event_type"], event["delivery_id"], event["data"])
    return "", 204
```

### Node (Express)

```js
const crypto = require('crypto')
const express = require('express')

const SECRET = process.env.CADENCE_WEBHOOK_SECRET
const TOLERANCE_SECONDS = 300
const app = express()

app.post('/cadence-webhook', express.raw({ type: '*/*' }), (req, res) => {
  const raw = req.body // Buffer, raw bytes
  const ts = req.get('X-Cadence-Timestamp') || ''
  const sig = req.get('X-Cadence-Signature') || ''

  if (!/^\d+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > TOLERANCE_SECONDS) {
    return res.status(401).send('stale or missing timestamp')
  }

  const bodyHash = crypto.createHash('sha256').update(raw).digest('hex')
  const expected = crypto.createHmac('sha256', SECRET).update(`${ts}\n${bodyHash}`).digest('hex')
  const ok =
    expected.length === sig.length &&
    crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sig))
  if (!ok) return res.status(401).send('bad signature')

  const event = JSON.parse(raw.toString('utf8'))
  console.log(event.event_type, event.delivery_id, event.data)
  res.status(204).end()
})
```

## Sending to Discord, Slack or Teams

Put a relay between Cadence and the chat tool:

```
Cadence  --> relay  --> Discord / Slack / Teams URL
   POST {event_type, timestamp, delivery_id, data}   (Cadence's generic, signed body)
                    POST {platform-specific message body}
```

The relay receives Cadence's generic JSON (verify the signature with the code
above), maps it to the target's message shape, and POSTs that to the target's
real incoming-webhook URL. In Cadence, the webhook URL you configure is the
**relay's** inbound URL, never the chat tool's.

You do not have to run a server for this. Options with a hosted inbound URL and
a place to write ~10 lines of mapping:

- **n8n** (n8n Cloud, or self-hosted): Webhook node -> Code/Set node -> HTTP
  Request node.
- **Make**: "Custom webhook" trigger -> "Create JSON" / "Set variable" -> HTTP
  module.
- **Pipedream**: HTTP trigger -> a Node or Python step -> HTTP request step.

### Example: `job.failed` to a Discord embed

Cadence delivers the `job.failed` body shown earlier. The relay transforms it
into what Discord's webhook API expects and POSTs **that** to the Discord URL:

```json
{
  "username": "Cadence",
  "embeds": [
    {
      "title": "apt_upgrade failed on web-01",
      "color": 15158332,
      "fields": [
        { "name": "Host", "value": "web-01", "inline": true },
        { "name": "Cause", "value": "dpkg_error", "inline": true },
        { "name": "Exit code", "value": "100", "inline": true }
      ],
      "description": "E: Sub-process /usr/bin/dpkg returned an error code (1)",
      "timestamp": "2026-01-15T09:42:11.503817Z"
    }
  ]
}
```

A minimal mapper (Pipedream / n8n "Code" step style):

```js
const e = steps.trigger.event.body // Cadence's JSON
const d = e.data

export default {
  username: 'Cadence',
  embeds: [
    {
      title: `${d.job_type} ${d.status} on ${d.hostname}`,
      color: d.status === 'failed' ? 0xe74c3c : 0x2ecc71,
      description: d.failure_summary || (d.log ? '```\n' + d.log.slice(-1500) + '\n```' : undefined),
      fields: [
        { name: 'Host', value: d.hostname, inline: true },
        d.failure_category && { name: 'Cause', value: d.failure_category, inline: true },
        { name: 'Exit code', value: String(d.exit_code), inline: true },
      ].filter(Boolean),
      timestamp: e.timestamp,
    },
  ],
}
```

For **Slack**, the target body is as simple as
`{ "text": "apt_upgrade failed on web-01 (exit 100)" }`, or Block Kit `blocks`
for structure. For **Teams**, post an Adaptive Card from a "When a Teams webhook
request is received" Workflows trigger.

Verifying Cadence's signature inside the relay is recommended but optional: if
the relay's inbound URL is unguessable and TLS-only and the feed is low value,
some operators skip it. If you skip it, anyone who learns the relay URL can
post messages to your channel.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| delivery `failed`, last error `HTTP 400` | the target wants its own body shape. You pointed the webhook at a chat URL directly; add a relay |
| `HTTP 401` / `HTTP 403` | the target rejected the request (wrong URL, revoked incoming webhook, missing auth the target needs) |
| `failed` with a connection / timeout error | the URL is unreachable from the Cadence server, or TLS does not verify |
| nothing arrives at all | the webhook is disabled, is not subscribed to that event, or `CADENCE_WEBHOOKS_ENABLED=false` |
| deliveries `failed` after a redirect | a redirecting URL is not supported; point the webhook at the final URL |
| the same event arrives twice | expected: delivery is at-least-once. Deduplicate on `delivery_id` |
