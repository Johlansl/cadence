import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { pill } from '../lib/pill'
import type { HostSummary, PackageExclusion, PolicyScope } from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const POLL_MS = 30_000

const field =
  'rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'
const primaryBtn =
  'rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500'
const ghostBtn =
  'rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200'

// Answers "what will Cadence never auto-upgrade, and where does that rule
// apply?" A rule is created or deleted, never edited in place.
export function ExclusionsView() {
  const [rows, setRows] = useState<PackageExclusion[] | null>(null)
  const [hosts, setHosts] = useState<HostSummary[]>([])
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setRows(await api.listExclusions())
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

  useEffect(() => {
    void api.listHosts().then(setHosts)
  }, [])

  const hostname = (id: string | null) =>
    id ? (hosts.find((h) => h.id === id)?.hostname ?? id) : null

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm uppercase tracking-widest text-zinc-400">Exclusions</h2>
      <p className="max-w-2xl text-xs text-zinc-500">
        Packages matching a rule here are never touched by an apt_upgrade job: Cadence holds them
        (dpkg <span className="font-mono text-[11px]">apt-mark hold</span>) and re-aligns dpkg's
        hold state to these rules on every run. Global rules apply everywhere; host rules apply to
        one host; tag rules apply to every host carrying that tag. All three add up, on top of each
        other.
      </p>

      <CreateForm hosts={hosts} onCreated={refresh} />

      {error && <p className="text-xs text-red-400">sync error: {error}</p>}

      {rows === null ? (
        <p className="text-sm text-zinc-600">loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-zinc-600">No exclusion rules configured.</p>
      ) : (
        <ul className="space-y-2">
          {rows.map((row) => (
            <li key={row.id}>
              <ExclusionRow exclusion={row} hostname={hostname(row.host_id)} onChanged={refresh} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CreateForm({ hosts, onCreated }: { hosts: HostSummary[]; onCreated: () => void }) {
  const [scope, setScope] = useState<PolicyScope>('global')
  const [hostId, setHostId] = useState('')
  const [tag, setTag] = useState('')
  const [pattern, setPattern] = useState('')
  const [description, setDescription] = useState('')
  const toast = useToast()

  const creator = useAdminKeyAction((key) =>
    api.createExclusion(key, {
      scope,
      host_id: scope === 'host' ? hostId : null,
      tag: scope === 'tag' ? tag.trim() : null,
      pattern: pattern.trim(),
      description: description.trim() || null,
    }),
  )

  const submit = async () => {
    const r = await creator.run()
    if (r?.ok) {
      setPattern('')
      setDescription('')
      setTag('')
      toast.notify('success', 'Exclusion rule created.')
      onCreated()
    }
  }

  const canSubmit =
    pattern.trim().length > 0 &&
    (scope === 'global' ||
      (scope === 'host' && hostId !== '') ||
      (scope === 'tag' && tag.trim() !== '')) &&
    !creator.busy

  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-3">
      <h3 className="text-xs uppercase tracking-wide text-zinc-600">Add a rule</h3>

      <div className="mt-2 flex flex-col gap-2">
        <div className="flex flex-wrap gap-2">
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as PolicyScope)}
            aria-label="Scope"
            className={field}
          >
            <option value="global">global (every host)</option>
            <option value="host">one host</option>
            <option value="tag">hosts with a tag</option>
          </select>
          {scope === 'host' && (
            <select
              value={hostId}
              onChange={(e) => setHostId(e.target.value)}
              aria-label="Host"
              className={field}
            >
              <option value="">select a host…</option>
              {hosts.map((h) => (
                <option key={h.id} value={h.id}>
                  {h.hostname}
                </option>
              ))}
            </select>
          )}
          {scope === 'tag' && (
            <input
              type="text"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              placeholder='"role=web" or "web"'
              aria-label="Tag"
              className={`${field} font-mono`}
            />
          )}
        </div>
        <input
          type="text"
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
          placeholder="package name or glob, e.g. linux-image*"
          aria-label="Pattern"
          className={`${field} font-mono`}
        />
        <input
          type="text"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="description (optional)"
          aria-label="Exclusion description"
          className={field}
        />
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

function ExclusionRow({
  exclusion,
  hostname,
  onChanged,
}: {
  exclusion: PackageExclusion
  hostname: string | null
  onChanged: () => void
}) {
  const toast = useToast()
  const confirm = useConfirm()
  const deleter = useAdminKeyAction((key) => api.deleteExclusion(exclusion.id, key))

  const onDelete = async () => {
    if (
      !(await confirm({
        title: 'Delete this exclusion rule?',
        body: `${exclusion.pattern} will be eligible for upgrades again on the next apt_upgrade run.`,
        confirmLabel: 'Delete',
        danger: true,
      }))
    )
      return
    const r = await deleter.run()
    if (r?.ok) {
      toast.notify('success', 'Exclusion rule deleted.')
      onChanged()
    }
  }

  return (
    <div className="rounded border border-zinc-800">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/40 px-3 py-1.5">
        <span className="truncate font-mono text-xs text-zinc-200">{exclusion.pattern}</span>
        <div className="flex items-center gap-2">
          <span
            className={pill(
              exclusion.scope === 'global'
                ? 'info'
                : exclusion.scope === 'tag'
                  ? 'warn'
                  : 'neutral',
            )}
          >
            {exclusion.scope === 'global'
              ? 'global'
              : exclusion.scope === 'tag'
                ? `tag: ${exclusion.tag}`
                : hostname}
          </span>
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
      <div className="space-y-1 px-3 py-2 text-xs text-zinc-500">
        {exclusion.description && <p className="text-zinc-400">{exclusion.description}</p>}
        <p>
          added <RelativeTime iso={exclusion.created_at} />
        </p>
      </div>
      <div className="px-3 pb-2">
        <AdminActionFeedback
          actions={deleter}
          onKeyAccepted={(r) => {
            if (r?.ok) onChanged()
          }}
        />
      </div>
    </div>
  )
}
