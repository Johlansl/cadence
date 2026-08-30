import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { pill } from '../lib/pill'
import type { HostDetail as HostDetailData, HostPackage, RebootPolicy } from '../types'
import { AdminKeyPrompt, useAdminKeyAction } from './AdminKeyPrompt'
import { Freshness } from './Freshness'
import { Jobs } from './Jobs'
import { Schedule } from './Schedule'
import { StatusBadge } from './StatusBadge'

function RebootPolicyControl({ hostId, value }: { hostId: string; value: RebootPolicy }) {
  const [choice, setChoice] = useState<RebootPolicy>(value)
  const choiceRef = useRef<RebootPolicy>(value)
  useEffect(() => {
    setChoice(value)
    choiceRef.current = value
  }, [value])

  const action = useCallback(
    (key: string) => api.patchHost(hostId, key, { reboot_policy: choiceRef.current }),
    [hostId],
  )
  const { run, submitKey, busy, error, needKey, keyDraft, setKeyDraft } = useAdminKeyAction(action)

  const change = (next: RebootPolicy) => {
    setChoice(next)
    choiceRef.current = next
    void run()
  }

  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-600">Reboot policy</dt>
      <dd className="mt-0.5 flex items-center gap-2 font-mono text-sm text-zinc-200">
        <select
          value={choice}
          disabled={busy}
          onChange={(e) => change(e.target.value as RebootPolicy)}
          className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 text-sm text-zinc-200 outline-none focus:border-zinc-500 disabled:opacity-50"
        >
          <option value="never">never</option>
          <option value="auto">auto</option>
        </select>
        {busy && <span className="text-xs text-zinc-500">saving…</span>}
      </dd>
      {needKey && (
        <AdminKeyPrompt
          value={keyDraft}
          onChange={setKeyDraft}
          onSubmit={() => void submitKey()}
        />
      )}
      {error && <p className="mt-1 text-xs text-red-400">{error}</p>}
    </div>
  )
}

function HostActions({
  hostId,
  isActive,
  onChanged,
  onDeleted,
}: {
  hostId: string
  isActive: boolean
  onChanged: () => void
  onDeleted: () => void
}) {
  const retire = useAdminKeyAction((key) =>
    api.patchHost(hostId, key, { is_active: !isActive }),
  )
  const remove = useAdminKeyAction((key) => api.deleteHost(hostId, key))

  const doRetire = async () => {
    const r = await retire.run()
    if (r?.ok) onChanged()
  }
  const doDelete = async () => {
    if (!window.confirm('Permanently delete this host and all its history?')) return
    const r = await remove.run()
    if (r?.ok) onDeleted()
  }
  const onKeySubmit = async () => {
    if (retire.needKey) {
      const r = await retire.submitKey()
      if (r?.ok) onChanged()
    } else {
      const r = await remove.submitKey()
      if (r?.ok) onDeleted()
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <div className="flex items-center gap-2 text-xs">
        <button
          type="button"
          onClick={() => void doRetire()}
          disabled={retire.busy}
          className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-400 hover:text-zinc-200 disabled:opacity-50"
        >
          {retire.busy ? '…' : isActive ? 'retire' : 'reactivate'}
        </button>
        <button
          type="button"
          onClick={() => void doDelete()}
          disabled={remove.busy}
          className="text-zinc-600 hover:text-red-400 disabled:opacity-50"
        >
          delete
        </button>
      </div>
      {(retire.needKey || remove.needKey) && (
        <AdminKeyPrompt
          value={retire.needKey ? retire.keyDraft : remove.keyDraft}
          onChange={retire.needKey ? retire.setKeyDraft : remove.setKeyDraft}
          onSubmit={() => void onKeySubmit()}
        />
      )}
      {(retire.error || remove.error) && (
        <p className="text-xs text-red-400">{retire.error ?? remove.error}</p>
      )}
    </div>
  )
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-zinc-600">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm text-zinc-200">{children}</dd>
    </div>
  )
}

// security update first, then any update, then the rest — name as tie-breaker.
function rank(p: HostPackage): number {
  if (p.candidate_version && p.is_security_update) return 0
  if (p.candidate_version) return 1
  return 2
}

export function HostDetail({
  host,
  onChanged,
  onDeleted,
}: {
  host: HostDetailData
  onChanged: () => void
  onDeleted: () => void
}) {
  const [onlyUpdates, setOnlyUpdates] = useState(true)

  const withUpdates = useMemo(
    () => host.packages.filter((p) => p.candidate_version).length,
    [host.packages],
  )

  const rows = useMemo(() => {
    const sorted = [...host.packages].sort(
      (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name),
    )
    return onlyUpdates ? sorted.filter((p) => p.candidate_version) : sorted
  }, [host.packages, onlyUpdates])

  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-zinc-800 px-6 py-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <h2 className="font-mono text-lg text-zinc-100">{host.hostname}</h2>
            <StatusBadge status={host.status} />
            {!host.is_active && (
              <span className="rounded bg-zinc-700/40 px-1.5 py-0.5 text-xs font-medium text-zinc-400 ring-1 ring-zinc-600">
                inactive
              </span>
            )}
            {host.reboot_required && <span className={pill('reboot')}>reboot required</span>}
          </div>
          <HostActions
            hostId={host.id}
            isActive={host.is_active}
            onChanged={onChanged}
            onDeleted={onDeleted}
          />
        </div>
        {host.description && <p className="mt-1 text-sm text-zinc-500">{host.description}</p>}
      </header>

      <div className="min-h-0 flex-1 overflow-auto">
        <dl className="grid grid-cols-2 gap-4 px-6 py-4 sm:grid-cols-3 lg:grid-cols-4">
          <Meta label="OS">
            {(host.os_name ?? host.os_family) + (host.os_version ? ` ${host.os_version}` : '')}
          </Meta>
          <Meta label="Agent">{host.agent_version ?? '—'}</Meta>
          <Meta label="Manager">{host.package_manager}</Meta>
          <Meta label="FQDN">{host.fqdn ?? '—'}</Meta>
          <Meta label="Last report">
            <Freshness iso={host.last_seen_at} />
          </Meta>
          <RebootPolicyControl hostId={host.id} value={host.reboot_policy} />
          <Meta label="Updates">
            {withUpdates}
            {host.security_updates_count > 0 && (
              <span className="text-red-400"> · {host.security_updates_count} security</span>
            )}
          </Meta>
        </dl>

        <Jobs hostId={host.id} />
        <Schedule hostId={host.id} />

        <div className="flex items-center justify-between border-t border-zinc-800 px-6 py-2 text-xs text-zinc-500">
          <span>
            {rows.length} package{rows.length === 1 ? '' : 's'} shown
            {onlyUpdates && withUpdates !== host.packages.length && ` (of ${host.packages.length})`}
          </span>
          <label className="flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              checked={onlyUpdates}
              onChange={(e) => setOnlyUpdates(e.target.checked)}
              className="accent-zinc-400"
            />
            only pending updates
          </label>
        </div>

        <table className="w-full text-left text-sm">
          <thead className="sticky top-0 bg-zinc-950 text-xs uppercase tracking-wide text-zinc-600">
            <tr>
              <th className="px-6 py-2 font-medium">Package</th>
              <th className="px-3 py-2 font-medium">Installed</th>
              <th className="px-3 py-2 font-medium">Candidate</th>
              <th className="px-3 py-2 font-medium">Origin</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-900 font-mono">
            {rows.map((p) => (
              <tr key={`${p.name}/${p.architecture}`} className="hover:bg-zinc-900/50">
                <td className="px-6 py-1.5 text-zinc-200">
                  {p.name}
                  <span className="text-zinc-600">:{p.architecture}</span>
                  {p.is_security_update && (
                    <span className="ml-2 rounded bg-red-500/10 px-1 font-sans text-[10px] font-medium text-red-400 ring-1 ring-red-500/30">
                      SEC
                    </span>
                  )}
                </td>
                <td className="px-3 py-1.5 text-zinc-500">{p.installed_version}</td>
                <td className="px-3 py-1.5 text-zinc-300">{p.candidate_version ?? '—'}</td>
                <td className="px-3 py-1.5 text-zinc-600">{p.update_origin ?? '—'}</td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={4} className="px-6 py-6 text-center font-sans text-zinc-600">
                  {onlyUpdates ? 'No pending updates.' : 'No packages reported.'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}
