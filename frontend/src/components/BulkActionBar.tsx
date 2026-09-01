import { useCallback, useState } from 'react'
import { api, type AdminWriteResult } from '../api/client'
import { clearAdminKey, getAdminKey, setAdminKey } from '../lib/adminKey'
import type { Job, RebootPolicy } from '../types'
import { AdminKeyPrompt } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { useToast } from './Toast'

type RebootChoice = 'default' | RebootPolicy

// Bulk actions over the checked hosts. Independent jobs, one POST per host --
// no multi-host sequencing (out of scope for V1, see docs/decisions.md).
export function BulkActionBar({
  hostIds,
  onClear,
  onDone,
}: {
  hostIds: string[]
  onClear: () => void
  onDone: () => void
}) {
  const toast = useToast()
  const confirm = useConfirm()
  const [reboot, setReboot] = useState<RebootChoice>('default')
  const [busy, setBusy] = useState<'' | 'upgrade' | 'reboot'>('')
  const [needKey, setNeedKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')
  // Retry the last-started action once the key is entered.
  const [retry, setRetry] = useState<null | 'upgrade' | 'reboot'>(null)

  const fanOut = useCallback(
    async (kind: 'upgrade' | 'reboot', key: string) => {
      setBusy(kind)
      const call = (id: string): Promise<AdminWriteResult<Job>> =>
        kind === 'reboot'
          ? api.rebootHost(id, key)
          : api.createJob(id, key, reboot === 'default' ? undefined : reboot)
      const results = await Promise.allSettled(hostIds.map(call))
      setBusy('')

      let ok = 0
      let busyHosts = 0
      let failed = 0
      let unauthorized = false
      for (const r of results) {
        if (r.status !== 'fulfilled') failed++
        else if (r.value.ok) ok++
        else if (r.value.status === 409) busyHosts++
        else if (r.value.status === 401) unauthorized = true
        else failed++
      }
      if (unauthorized) {
        clearAdminKey()
        setNeedKey(true)
        setRetry(kind)
        toast.notify('error', 'Invalid admin key.')
        return
      }
      const parts = [`${ok} queued`]
      if (busyHosts) parts.push(`${busyHosts} already busy`)
      if (failed) parts.push(`${failed} failed`)
      const verb = kind === 'reboot' ? 'Bulk reboot' : 'Bulk upgrade'
      toast.notify(failed ? 'error' : 'success', `${verb}: ${parts.join(', ')}.`)
      onDone()
    },
    [hostIds, reboot, toast, onDone],
  )

  const start = async (kind: 'upgrade' | 'reboot') => {
    if (
      kind === 'reboot' &&
      !(await confirm({
        title: `Reboot ${hostIds.length} host${hostIds.length === 1 ? '' : 's'}?`,
        body: 'Queues a reboot job on every selected host. Each reboots as its agent picks the job up.',
        confirmLabel: 'Reboot all',
        danger: true,
      }))
    )
      return
    const key = getAdminKey()
    if (!key) {
      setNeedKey(true)
      setRetry(kind)
      return
    }
    void fanOut(kind, key)
  }

  const submitKey = () => {
    if (!keyDraft.trim()) return
    setAdminKey(keyDraft.trim())
    setKeyDraft('')
    setNeedKey(false)
    const kind = retry
    setRetry(null)
    if (kind) void fanOut(kind, keyDraft.trim())
  }

  return (
    <div className="border-b border-zinc-800 bg-zinc-900/60 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-zinc-300">{hostIds.length} selected</span>
        <label className="flex items-center gap-1 text-zinc-500">
          reboot
          <select
            value={reboot}
            onChange={(e) => setReboot(e.target.value as RebootChoice)}
            className="rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 text-xs text-zinc-200 outline-none focus:border-zinc-500"
          >
            <option value="default">host default</option>
            <option value="auto">auto</option>
            <option value="never">never</option>
            <option value="prompt">prompt</option>
          </select>
        </label>
        <button
          type="button"
          onClick={() => void start('upgrade')}
          disabled={busy !== ''}
          className="rounded bg-zinc-100 px-2 py-1 font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500"
        >
          {busy === 'upgrade' ? 'queuing…' : 'trigger dist-upgrade'}
        </button>
        <button
          type="button"
          onClick={() => void start('reboot')}
          disabled={busy !== ''}
          className="rounded border border-orange-500/40 px-2 py-1 text-orange-300 hover:bg-orange-500/10 disabled:opacity-50"
        >
          {busy === 'reboot' ? 'queuing…' : 'reboot selected'}
        </button>
        <button type="button" onClick={onClear} className="text-zinc-500 hover:text-zinc-300">
          clear
        </button>
      </div>
      {needKey && <AdminKeyPrompt value={keyDraft} onChange={setKeyDraft} onSubmit={submitKey} />}
    </div>
  )
}
