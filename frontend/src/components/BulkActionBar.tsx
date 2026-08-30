import { useCallback, useState } from 'react'
import { api } from '../api/client'
import { clearAdminKey, getAdminKey, setAdminKey } from '../lib/adminKey'
import type { RebootPolicy } from '../types'
import { AdminKeyPrompt } from './AdminKeyPrompt'
import { useToast } from './Toast'

type RebootChoice = 'default' | RebootPolicy

// Bulk "trigger dist-upgrade" over the checked hosts. Independent jobs, one
// POST per host -- no multi-host sequencing (out of scope, CLAUDE.md section 2).
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
  const [reboot, setReboot] = useState<RebootChoice>('default')
  const [busy, setBusy] = useState(false)
  const [needKey, setNeedKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')

  const run = useCallback(async () => {
    const key = getAdminKey()
    if (!key) {
      setNeedKey(true)
      return
    }
    setBusy(true)
    const results = await Promise.allSettled(
      hostIds.map((id) =>
        api.createJob(id, key, reboot === 'default' ? undefined : reboot),
      ),
    )
    setBusy(false)

    let ok = 0
    let busyHosts = 0
    let failed = 0
    let unauthorized = false
    for (const r of results) {
      if (r.status !== 'fulfilled') {
        failed++
      } else if (r.value.ok) {
        ok++
      } else if (r.value.status === 409) {
        busyHosts++
      } else if (r.value.status === 401) {
        unauthorized = true
      } else {
        failed++
      }
    }

    if (unauthorized) {
      clearAdminKey()
      setNeedKey(true)
      toast.notify('error', 'Invalid admin key.')
      return
    }
    const parts = [`${ok} queued`]
    if (busyHosts) parts.push(`${busyHosts} already busy`)
    if (failed) parts.push(`${failed} failed`)
    toast.notify(failed ? 'error' : 'success', `Bulk upgrade: ${parts.join(', ')}.`)
    onDone()
  }, [hostIds, reboot, toast, onDone])

  const submitKey = () => {
    if (!keyDraft.trim()) return
    setAdminKey(keyDraft.trim())
    setKeyDraft('')
    setNeedKey(false)
    void run()
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
          </select>
        </label>
        <button
          type="button"
          onClick={() => void run()}
          disabled={busy}
          className="rounded bg-zinc-100 px-2 py-1 font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500"
        >
          {busy ? 'queuing…' : 'trigger dist-upgrade'}
        </button>
        <button type="button" onClick={onClear} className="text-zinc-500 hover:text-zinc-300">
          clear
        </button>
      </div>
      {needKey && (
        <AdminKeyPrompt value={keyDraft} onChange={setKeyDraft} onSubmit={submitKey} />
      )}
    </div>
  )
}
