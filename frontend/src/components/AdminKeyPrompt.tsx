import { useCallback, useState } from 'react'
import type { AdminWriteResult } from '../api/client'
import { clearAdminKey, getAdminKey, setAdminKey } from '../lib/adminKey'

// Shared plumbing for the admin-key-guarded write endpoints (trigger a job,
// change a host's reboot policy, ...). Holds the busy/error state and the
// "enter the key" flow; the caller supplies the actual request.
export function useAdminKeyAction<T>(action: (key: string) => Promise<AdminWriteResult<T>>) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [needKey, setNeedKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')

  const run = useCallback(async (): Promise<AdminWriteResult<T> | undefined> => {
    const key = getAdminKey()
    if (!key) {
      setNeedKey(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await action(key)
      if (res.ok) {
        setNeedKey(false)
      } else if (res.status === 401) {
        clearAdminKey()
        setNeedKey(true)
        setError('Invalid admin key.')
      } else {
        setError(res.detail ?? `Request failed (${res.status}).`)
      }
      return res
    } finally {
      setBusy(false)
    }
  }, [action])

  const submitKey = useCallback(async (): Promise<AdminWriteResult<T> | undefined> => {
    if (!keyDraft.trim()) return
    setAdminKey(keyDraft.trim())
    setKeyDraft('')
    setNeedKey(false)
    return run()
  }, [keyDraft, run])

  return { run, submitKey, busy, error, setError, needKey, keyDraft, setKeyDraft }
}

export function AdminKeyPrompt({
  value,
  onChange,
  onSubmit,
}: {
  value: string
  onChange: (v: string) => void
  onSubmit: () => void
}) {
  return (
    <div className="mt-2 flex items-center gap-2">
      <input
        type="password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') onSubmit()
        }}
        placeholder="X-Admin-Key"
        autoFocus
        className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      />
      <button
        type="button"
        onClick={onSubmit}
        className="rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white"
      >
        save &amp; retry
      </button>
    </div>
  )
}
