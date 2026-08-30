import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { pill } from '../lib/pill'
import type { HostDetail as HostDetailData, RebootPolicy } from '../types'
import { AdminKeyPrompt, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { Freshness } from './Freshness'
import { HostHistory } from './HostHistory'
import { Jobs } from './Jobs'
import { PackageTable } from './PackageTable'
import { Schedule } from './Schedule'
import { StatusBadge } from './StatusBadge'
import { TagChips } from './TagChips'
import { useToast } from './Toast'

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

function sameTags(a: Record<string, string>, b: Record<string, string>): boolean {
  const ak = Object.keys(a)
  return ak.length === Object.keys(b).length && ak.every((k) => a[k] === b[k])
}

function TagsControl({
  hostId,
  tags,
  onChanged,
}: {
  hostId: string
  tags: Record<string, string>
  onChanged: () => void
}) {
  const [draft, setDraft] = useState<Record<string, string>>(tags)
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  const draftRef = useRef(draft)
  const toast = useToast()

  // Reset when the host (or its saved tags) changes underneath us.
  useEffect(() => {
    setDraft(tags)
    draftRef.current = tags
    setNewKey('')
    setNewValue('')
  }, [hostId, tags])

  const update = (next: Record<string, string>) => {
    setDraft(next)
    draftRef.current = next
  }
  const addPair = () => {
    const k = newKey.trim()
    if (!k) return
    update({ ...draftRef.current, [k]: newValue.trim() })
    setNewKey('')
    setNewValue('')
  }
  const removeKey = (k: string) => {
    const next = { ...draftRef.current }
    delete next[k]
    update(next)
  }

  const action = useCallback(
    (key: string) => api.patchHost(hostId, key, { tags: draftRef.current }),
    [hostId],
  )
  const { run, submitKey, busy, error, needKey, keyDraft, setKeyDraft } = useAdminKeyAction(action)

  const save = async () => {
    const r = await run()
    if (r?.ok) {
      toast.notify('success', 'Tags saved.')
      onChanged()
    }
  }
  const onKeySubmit = async () => {
    const r = await submitKey()
    if (r?.ok) {
      toast.notify('success', 'Tags saved.')
      onChanged()
    }
  }

  const dirty = !sameTags(draft, tags)
  const inputCls =
    'rounded border border-zinc-700 bg-zinc-900 px-1.5 py-0.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500'

  return (
    <section className="border-t border-zinc-800 px-6 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wide text-zinc-600">Tags</h3>
        {dirty && (
          <button
            type="button"
            onClick={() => void save()}
            disabled={busy}
            className="rounded bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500"
          >
            {busy ? 'saving…' : 'save tags'}
          </button>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {Object.entries(draft).length === 0 && (
          <span className="text-xs text-zinc-600">none</span>
        )}
        {Object.entries(draft).map(([k, v]) => (
          <span
            key={k}
            className="flex items-center gap-1 rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 ring-1 ring-zinc-700"
          >
            {v ? `${k}=${v}` : k}
            <button
              type="button"
              onClick={() => removeKey(k)}
              aria-label={`Remove tag ${k}`}
              className="text-zinc-500 hover:text-red-400"
            >
              ×
            </button>
          </span>
        ))}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
        <input
          value={newKey}
          onChange={(e) => setNewKey(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addPair()}
          placeholder="key"
          aria-label="New tag key"
          className={`${inputCls} w-28`}
        />
        <input
          value={newValue}
          onChange={(e) => setNewValue(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && addPair()}
          placeholder="value"
          aria-label="New tag value"
          className={`${inputCls} w-32`}
        />
        <button
          type="button"
          onClick={addPair}
          className="rounded border border-zinc-700 px-2 py-0.5 text-zinc-400 hover:text-zinc-200"
        >
          add
        </button>
      </div>

      {needKey && (
        <AdminKeyPrompt value={keyDraft} onChange={setKeyDraft} onSubmit={() => void onKeySubmit()} />
      )}
      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}
    </section>
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
  const confirm = useConfirm()
  const toast = useToast()
  const retire = useAdminKeyAction((key) =>
    api.patchHost(hostId, key, { is_active: !isActive }),
  )
  const remove = useAdminKeyAction((key) => api.deleteHost(hostId, key))

  const doRetire = async () => {
    if (
      isActive &&
      !(await confirm({
        title: 'Retire this host?',
        body: 'It stops counting toward fleet health and reports are rejected until reactivated. History is kept.',
        confirmLabel: 'Retire',
      }))
    )
      return
    const r = await retire.run()
    if (r?.ok) {
      toast.notify('success', isActive ? 'Host retired.' : 'Host reactivated.')
      onChanged()
    }
  }
  const doDelete = async () => {
    if (
      !(await confirm({
        title: 'Delete this host?',
        body: 'Permanently removes the host and all its packages, reports, jobs and schedule. This cannot be undone.',
        confirmLabel: 'Delete',
        danger: true,
      }))
    )
      return
    const r = await remove.run()
    if (r?.ok) {
      toast.notify('success', 'Host deleted.')
      onDeleted()
    }
  }
  const onKeySubmit = async () => {
    if (retire.needKey) {
      const r = await retire.submitKey()
      if (r?.ok) {
        toast.notify('success', isActive ? 'Host retired.' : 'Host reactivated.')
        onChanged()
      }
    } else {
      const r = await remove.submitKey()
      if (r?.ok) {
        toast.notify('success', 'Host deleted.')
        onDeleted()
      }
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

export function HostDetail({
  host,
  onChanged,
  onDeleted,
}: {
  host: HostDetailData
  onChanged: () => void
  onDeleted: () => void
}) {
  const withUpdates = useMemo(
    () => host.packages.filter((p) => p.candidate_version).length,
    [host.packages],
  )

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
        {Object.keys(host.tags).length > 0 && (
          <div className="mt-2">
            <TagChips tags={host.tags} />
          </div>
        )}
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

        <TagsControl hostId={host.id} tags={host.tags} onChanged={onChanged} />
        <HostHistory hostId={host.id} />
        <Jobs hostId={host.id} />
        <Schedule hostId={host.id} />

        <PackageTable packages={host.packages} />
      </div>
    </div>
  )
}
