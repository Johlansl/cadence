import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { pill } from '../lib/pill'
import type { Webhook, WebhookCreated, WebhookEvent } from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const POLL_MS = 30_000

const ALL_EVENTS: WebhookEvent[] = [
  'job.succeeded',
  'job.failed',
  'host.offline',
  'host.reboot_required',
  'host.security_updates_available',
]

const chip =
  'rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 ring-1 ring-zinc-700'
const field =
  'rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'
const primaryBtn =
  'rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500'
const ghostBtn =
  'rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200'

// Answers "what is Cadence configured to notify, and is it working?" The full
// URL and the signing secret are shown only in the create response.
export function WebhooksView() {
  const [rows, setRows] = useState<Webhook[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setRows(await api.listWebhooks())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm uppercase tracking-widest text-zinc-400">Webhooks</h2>
      <p className="max-w-2xl text-xs text-zinc-500">
        Cadence POSTs a signed JSON body to each enabled endpoint when a subscribed event fires. The
        full URL and the signing secret are shown once, at creation.
      </p>

      <CreateForm onCreated={refresh} />

      {error && <p className="text-xs text-red-400">sync error: {error}</p>}

      {rows === null ? (
        <p className="text-sm text-zinc-600">loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-zinc-600">No webhooks configured.</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((w) => (
            <li key={w.id}>
              <WebhookRow webhook={w} onChanged={refresh} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CreateForm({ onCreated }: { onCreated: () => void }) {
  const [url, setUrl] = useState('')
  const [events, setEvents] = useState<WebhookEvent[]>([])
  const [description, setDescription] = useState('')
  const [created, setCreated] = useState<WebhookCreated | null>(null)
  const toast = useToast()

  const creator = useAdminKeyAction((key) =>
    api.createWebhook(key, {
      url: url.trim(),
      event_types: events,
      enabled: true,
      description: description.trim() || null,
    }),
  )

  const toggleEvent = (e: WebhookEvent) =>
    setEvents((cur) => (cur.includes(e) ? cur.filter((x) => x !== e) : [...cur, e]))

  const submit = async () => {
    const r = await creator.run()
    if (r?.ok && r.data) {
      setCreated(r.data)
      setUrl('')
      setEvents([])
      setDescription('')
      toast.notify('success', 'Webhook created.')
      onCreated()
    }
  }

  const canSubmit = url.trim().length > 0 && events.length > 0 && !creator.busy

  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-3">
      <h3 className="text-xs uppercase tracking-wide text-zinc-600">Add a webhook</h3>

      {created && (
        <div className="mt-2 rounded border border-emerald-500/30 bg-emerald-500/10 p-2 text-xs">
          <p className="text-emerald-300">Copy the secret now, it is not shown again.</p>
          <p className="mt-1 break-all text-zinc-300">
            URL <span className="font-mono text-zinc-100">{created.url}</span>
          </p>
          <p className="mt-0.5 break-all text-zinc-300">
            Secret <span className="font-mono text-zinc-100">{created.secret}</span>
          </p>
          <button
            type="button"
            onClick={() => setCreated(null)}
            className="mt-1 text-zinc-500 hover:text-zinc-300"
          >
            dismiss
          </button>
        </div>
      )}

      <div className="mt-2 flex flex-col gap-2">
        <input
          type="url"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="https://example.com/hook"
          aria-label="Webhook URL"
          className={`${field} font-mono`}
        />
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="description (optional)"
          aria-label="Webhook description"
          className={field}
        />
        <fieldset className="flex flex-wrap gap-x-4 gap-y-1">
          <legend className="text-xs text-zinc-600">events</legend>
          {ALL_EVENTS.map((e) => (
            <label key={e} className="flex select-none items-center gap-1 text-xs text-zinc-400">
              <input
                type="checkbox"
                checked={events.includes(e)}
                onChange={() => toggleEvent(e)}
                className="accent-zinc-400"
              />
              <span className="font-mono text-[11px]">{e}</span>
            </label>
          ))}
        </fieldset>
        <div>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className={primaryBtn}
          >
            {creator.busy ? 'creating…' : 'create'}
          </button>
        </div>
      </div>

      <AdminActionFeedback actions={creator} />
    </section>
  )
}

function WebhookRow({ webhook, onChanged }: { webhook: Webhook; onChanged: () => void }) {
  const toast = useToast()
  const confirm = useConfirm()

  const toggler = useAdminKeyAction((key) =>
    api.updateWebhook(webhook.id, key, { enabled: !webhook.enabled }),
  )
  const tester = useAdminKeyAction((key) => api.testWebhook(webhook.id, key))
  const deleter = useAdminKeyAction((key) => api.deleteWebhook(webhook.id, key))

  const onToggle = async () => {
    const r = await toggler.run()
    if (r?.ok) {
      toast.notify('success', webhook.enabled ? 'Webhook disabled.' : 'Webhook enabled.')
      onChanged()
    }
  }
  const onTest = async () => {
    const r = await tester.run()
    if (r?.ok) toast.notify('success', 'Test delivery queued.')
  }
  const onDelete = async () => {
    if (
      !(await confirm({
        title: 'Delete this webhook?',
        body: `${webhook.url_preview} will stop receiving events.`,
        confirmLabel: 'Delete',
        danger: true,
      }))
    )
      return
    const r = await deleter.run()
    if (r?.ok) {
      toast.notify('success', 'Webhook deleted.')
      onChanged()
    }
  }

  return (
    <div className="rounded border border-zinc-800">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/40 px-3 py-1.5">
        <span className="truncate font-mono text-xs text-zinc-200">{webhook.url_preview}</span>
        <div className="flex items-center gap-2">
          <span className={pill(webhook.enabled ? 'ok' : 'neutral')}>
            {webhook.enabled ? 'enabled' : 'disabled'}
          </span>
          <button
            type="button"
            onClick={() => void onToggle()}
            disabled={toggler.busy}
            className={ghostBtn}
          >
            {webhook.enabled ? 'disable' : 'enable'}
          </button>
          <button
            type="button"
            onClick={() => void onTest()}
            disabled={tester.busy}
            className={ghostBtn}
          >
            {tester.busy ? 'sending…' : 'send test'}
          </button>
          <button
            type="button"
            onClick={() => void onDelete()}
            disabled={deleter.busy}
            className={ghostBtn}
          >
            delete
          </button>
        </div>
      </div>

      <div className="space-y-1.5 px-3 py-2 text-xs text-zinc-500">
        {webhook.description && <p className="text-zinc-400">{webhook.description}</p>}
        <div className="flex flex-wrap gap-1">
          {webhook.event_types.map((e) => (
            <span key={e} className={chip}>
              {e}
            </span>
          ))}
        </div>
        <p>
          {webhook.last_success_at ? (
            <>
              last delivered <RelativeTime iso={webhook.last_success_at} />
            </>
          ) : (
            'no successful delivery yet'
          )}
          {webhook.pending_count > 0 && <span> · {webhook.pending_count} pending</span>}
          {webhook.failed_count > 0 && (
            <span className="text-red-400"> · {webhook.failed_count} failed</span>
          )}
        </p>
        {webhook.last_error && <p className="text-red-400">last error: {webhook.last_error}</p>}
      </div>

      <div className="px-3 pb-2">
        <AdminActionFeedback
          actions={[toggler, tester, deleter]}
          onKeyAccepted={(r, a) => {
            if (r?.ok && a !== tester) onChanged()
          }}
        />
      </div>
    </div>
  )
}
