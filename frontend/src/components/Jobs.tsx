import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { clearAdminKey, getAdminKey, setAdminKey } from '../lib/adminKey'
import { relativeTime } from '../lib/time'
import type { Job, JobStatus } from '../types'

const STATUS_CLS: Record<JobStatus, string> = {
  pending: 'bg-zinc-500/10 text-zinc-400 ring-zinc-500/30',
  running: 'bg-sky-500/10 text-sky-400 ring-sky-500/30',
  succeeded: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/30',
  failed: 'bg-red-500/10 text-red-400 ring-red-500/30',
}

function JobBadge({ status }: { status: JobStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ring-1 ${STATUS_CLS[status]}`}
    >
      {status}
    </span>
  )
}

function duration(from: string | null, to: string | null): string | null {
  if (!from || !to) return null
  const s = Math.round((new Date(to).getTime() - new Date(from).getTime()) / 1000)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s`
}

export function Jobs({ hostId }: { hostId: string }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [needKey, setNeedKey] = useState(false)
  const [keyDraft, setKeyDraft] = useState('')

  const refresh = useCallback(async () => {
    try {
      setJobs(await api.getHostJobs(hostId))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [hostId])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), 15_000)
    return () => clearInterval(t)
  }, [refresh])

  const active = jobs.some((j) => j.status === 'pending' || j.status === 'running')

  const trigger = useCallback(async () => {
    const key = getAdminKey()
    if (!key) {
      setNeedKey(true)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const res = await api.createJob(hostId, key)
      if (res.ok) {
        setNeedKey(false)
      } else if (res.status === 401) {
        clearAdminKey()
        setNeedKey(true)
        setError('Invalid admin key.')
      } else {
        setError(res.detail ?? `Request failed (${res.status}).`)
      }
      await refresh()
    } finally {
      setBusy(false)
    }
  }, [hostId, refresh])

  const saveKeyAndTrigger = useCallback(async () => {
    if (!keyDraft.trim()) return
    setAdminKey(keyDraft.trim())
    setKeyDraft('')
    setNeedKey(false)
    await trigger()
  }, [keyDraft, trigger])

  return (
    <section className="border-t border-zinc-800 px-6 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wide text-zinc-600">Jobs</h3>
        <button
          type="button"
          onClick={() => void trigger()}
          disabled={busy || active}
          className="rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
        >
          {active ? 'job in progress…' : busy ? 'triggering…' : 'trigger dist-upgrade'}
        </button>
      </div>

      {needKey && (
        <div className="mt-2 flex items-center gap-2">
          <input
            type="password"
            value={keyDraft}
            onChange={(e) => setKeyDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') void saveKeyAndTrigger()
            }}
            placeholder="X-Admin-Key"
            autoFocus
            className="flex-1 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
          />
          <button
            type="button"
            onClick={() => void saveKeyAndTrigger()}
            className="rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white"
          >
            save &amp; trigger
          </button>
        </div>
      )}

      {error && <p className="mt-2 text-xs text-red-400">{error}</p>}

      {jobs.length === 0 ? (
        <p className="mt-2 text-xs text-zinc-600">No jobs yet.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {jobs.map((j) => (
            <li
              key={j.id}
              className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-xs"
            >
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-zinc-400">
                <JobBadge status={j.status} />
                <span className="font-mono text-zinc-300">{j.job_type}</span>
                <span>· {relativeTime(j.created_at)}</span>
                {j.requested_by && <span>· by {j.requested_by}</span>}
                {duration(j.started_at, j.completed_at) && (
                  <span>· {duration(j.started_at, j.completed_at)}</span>
                )}
                {j.result?.exit_code != null && <span>· exit {j.result.exit_code}</span>}
                {j.result?.reboot_required && (
                  <span className="text-orange-400">· reboot required</span>
                )}
                {j.status === 'pending' && (
                  <span className="text-zinc-600">· the agent picks it up within ~1 min</span>
                )}
              </div>
              {j.log && (
                <details className="mt-1">
                  <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">log</summary>
                  <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 font-mono text-[11px] leading-relaxed text-zinc-400">
                    {j.log}
                  </pre>
                </details>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
