import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { relativeTime } from '../lib/time'
import type { Job, JobStatus, RebootPolicy } from '../types'
import { AdminKeyPrompt, useAdminKeyAction } from './AdminKeyPrompt'

const STATUS_CLS: Record<JobStatus, string> = {
  pending: 'bg-zinc-500/10 text-zinc-400 ring-zinc-500/30',
  running: 'bg-sky-500/10 text-sky-400 ring-sky-500/30',
  succeeded: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/30',
  failed: 'bg-red-500/10 text-red-400 ring-red-500/30',
}

const COLLAPSE_KEY = 'cadence.jobs.collapsed'
const PREVIEW = 5

function readCollapsed(): boolean {
  try {
    return localStorage.getItem(COLLAPSE_KEY) === '1'
  } catch {
    return false
  }
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

type RebootChoice = 'default' | RebootPolicy

export function Jobs({ hostId }: { hostId: string }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [reboot, setReboot] = useState<RebootChoice>('default')
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const [showAll, setShowAll] = useState(false)

  const refresh = useCallback(async () => {
    try {
      setJobs(await api.getHostJobs(hostId))
    } catch {
      /* transient; the next poll retries */
    }
  }, [hostId])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), 15_000)
    return () => clearInterval(t)
  }, [refresh])

  const toggleCollapsed = () => {
    setCollapsed((c) => {
      const next = !c
      try {
        localStorage.setItem(COLLAPSE_KEY, next ? '1' : '0')
      } catch {
        /* ignore */
      }
      return next
    })
  }

  const active = jobs.some((j) => j.status === 'pending' || j.status === 'running')

  const createJob = useCallback(
    (key: string) => api.createJob(hostId, key, reboot === 'default' ? undefined : reboot),
    [hostId, reboot],
  )
  const trig = useAdminKeyAction(createJob)
  const clear = useAdminKeyAction((key) => api.clearHostJobs(hostId, key))

  const trigger = useCallback(async () => {
    await trig.run()
    await refresh()
  }, [trig, refresh])

  const doClear = useCallback(async () => {
    if (!window.confirm('Delete every job for this host? This cannot be undone.')) return
    const r = await clear.run()
    if (r?.ok) await refresh()
  }, [clear, refresh])

  const onKeySubmit = useCallback(async () => {
    if (trig.needKey) await trig.submitKey()
    else await clear.submitKey()
    await refresh()
  }, [trig, clear, refresh])

  const shown = showAll ? jobs : jobs.slice(0, PREVIEW)
  const last = jobs[0]

  return (
    <section className="border-t border-zinc-800 px-6 py-4">
      <div className="flex items-center justify-between gap-3">
        <button
          type="button"
          onClick={toggleCollapsed}
          className="flex items-center gap-1.5 text-xs uppercase tracking-wide text-zinc-600 hover:text-zinc-400"
        >
          <span className="inline-block w-2 text-zinc-500">{collapsed ? '▸' : '▾'}</span>
          Jobs
          <span className="text-zinc-700">({jobs.length})</span>
        </button>

        {!collapsed && (
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-1 text-xs text-zinc-500">
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
              onClick={() => void trigger()}
              disabled={trig.busy || active}
              className="rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-500"
            >
              {active ? 'job in progress…' : trig.busy ? 'triggering…' : 'trigger dist-upgrade'}
            </button>
            {jobs.length > 0 && (
              <button
                type="button"
                onClick={() => void doClear()}
                disabled={clear.busy}
                className="text-xs text-zinc-600 hover:text-red-400 disabled:opacity-50"
              >
                clear
              </button>
            )}
          </div>
        )}
      </div>

      {collapsed ? (
        <p className="mt-1 text-xs text-zinc-600">
          {last
            ? `last ${last.status} ${relativeTime(last.created_at)}`
            : 'no jobs yet'}
        </p>
      ) : (
        <>
          {(trig.needKey || clear.needKey) && (
            <AdminKeyPrompt
              value={trig.needKey ? trig.keyDraft : clear.keyDraft}
              onChange={trig.needKey ? trig.setKeyDraft : clear.setKeyDraft}
              onSubmit={() => void onKeySubmit()}
            />
          )}
          {(trig.error || clear.error) && (
            <p className="mt-2 text-xs text-red-400">{trig.error ?? clear.error}</p>
          )}

          {jobs.length === 0 ? (
            <p className="mt-2 text-xs text-zinc-600">No jobs yet.</p>
          ) : (
            <>
              <ul className="mt-2 space-y-2">
                {shown.map((j) => (
                  <li
                    key={j.id}
                    className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2 text-xs"
                  >
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-zinc-400">
                      <JobBadge status={j.status} />
                      <span className="font-mono text-zinc-300">{j.job_type}</span>
                      <span>· {relativeTime(j.created_at)}</span>
                      {j.requested_by && <span>· by {j.requested_by}</span>}
                      {typeof j.params.reboot === 'string' && (
                        <span>· reboot {j.params.reboot}</span>
                      )}
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
                        <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
                          log
                        </summary>
                        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-zinc-950 p-2 font-mono text-[11px] leading-relaxed text-zinc-400">
                          {j.log}
                        </pre>
                      </details>
                    )}
                  </li>
                ))}
              </ul>
              {jobs.length > PREVIEW && (
                <button
                  type="button"
                  onClick={() => setShowAll((v) => !v)}
                  className="mt-2 text-xs text-zinc-500 hover:text-zinc-300"
                >
                  {showAll ? 'show less' : `show all (${jobs.length})`}
                </button>
              )}
            </>
          )}
        </>
      )}
    </section>
  )
}
