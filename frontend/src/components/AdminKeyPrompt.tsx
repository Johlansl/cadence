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

export type AdminKeyAction = ReturnType<typeof useAdminKeyAction>

// Renders the "enter the admin key" prompt for whichever of `actions` is
// currently asking for it, plus the first error among them. Replaces the
// `{(a.needKey || b.needKey) && <AdminKeyPrompt value={a.needKey ? ... : ...}/>}`
// boilerplate that every admin-guarded control used to repeat.
export function AdminActionFeedback({
  actions,
  onKeyAccepted,
  errorClassName = 'mt-2 text-xs text-red-400',
}: {
  actions: AdminKeyAction | AdminKeyAction[]
  // Called after the key is saved and the pending action retried, with its
  // result and the action that ran (identity-comparable to disambiguate).
  onKeyAccepted?: (res: AdminWriteResult<unknown> | undefined, action: AdminKeyAction) => void
  errorClassName?: string
}) {
  const list = Array.isArray(actions) ? actions : [actions]
  const active = list.find((a) => a.needKey)
  const error = list.map((a) => a.error).find((e): e is string => Boolean(e))
  return (
    <>
      {active && (
        <AdminKeyPrompt
          value={active.keyDraft}
          onChange={active.setKeyDraft}
          onSubmit={() => {
            void active.submitKey().then((res) => onKeyAccepted?.(res, active))
          }}
        />
      )}
      {error && <p className={errorClassName}>{error}</p>}
    </>
  )
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
