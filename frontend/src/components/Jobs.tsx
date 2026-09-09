import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { pill, type Tone } from '../lib/pill'
import type { Job, JobStatus, RebootPolicy } from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const STATUS_TONE: Record<JobStatus, Tone> = {
  pending: 'neutral',
  running: 'info',
  succeeded: 'ok',
  failed: 'danger',
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
  return <span className={pill(STATUS_TONE[status])}>{status}</span>
}

// Failure categories the agent / reaper set on a failed job (server roadmap
// item 2). Unknown values fall back to a neutral pill.
const FAILURE_TONE: Record<string, Tone> = {
  apt_locked: 'warn',
  network_or_repo: 'warn',
  timeout: 'warn',
  dpkg_error: 'danger',
  disk_full: 'danger',
  agent_lost: 'neutral',
  agent_refused: 'neutral',
  unknown: 'neutral',
}

function FailureBadge({ category, summary }: { category: string; summary: string | null }) {
  return (
    <span className={pill(FAILURE_TONE[category] ?? 'neutral')} title={summary ?? undefined}>
      {category.replace(/_/g, ' ')}
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
  const [stale, setStale] = useState(false)
  // Pages fetched past the polled first page, and whether the tail is reached.
  const [older, setOlder] = useState<Job[]>([])
  const [exhausted, setExhausted] = useState(false)

  const PAGE = 20

  // Force-refresh after a user action; result is always applied.
  const refresh = useCallback(async () => {
    try {
      setJobs(await api.getHostJobs(hostId, { limit: PAGE }))
      setStale(false)
    } catch {
      setStale(true)
    }
  }, [hostId])

  // Background poll, guarded so a slow response for a host we've navigated
  // away from can't overwrite the new host's jobs.
  useEffect(() => {
    setOlder([])
    setExhausted(false)
    let cancelled = false
    const poll = async () => {
      try {
        const j = await api.getHostJobs(hostId, { limit: PAGE })
        if (!cancelled) {
          setJobs(j)
          setStale(false)
        }
      } catch {
        if (!cancelled) setStale(true)
      }
    }
    void poll()
    const t = setInterval(poll, 15_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [hostId])

  const allJobs = useMemo(() => (older.length ? [...jobs, ...older] : jobs), [jobs, older])

  const loadOlder = useCallback(async () => {
    const ref = allJobs[allJobs.length - 1]
    if (!ref) return
    try {
      const more = await api.getHostJobs(hostId, { before: ref.created_at, limit: PAGE })
      setOlder((o) => [...o, ...more])
      if (more.length < PAGE) setExhausted(true)
    } catch {
      setStale(true)
    }
  }, [hostId, allJobs])

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
  const confirm = useConfirm()
  const toast = useToast()

  const trigger = useCallback(async () => {
    const r = await trig.run()
    if (r?.ok) toast.notify('success', 'Upgrade job queued.')
    await refresh()
  }, [trig, toast, refresh])

  const doClear = useCallback(async () => {
    if (
      !(await confirm({
        title: 'Clear job history?',
        body: 'Deletes every job row for this host. This cannot be undone.',
        confirmLabel: 'Clear',
        danger: true,
      }))
    )
      return
    const r = await clear.run()
    if (r?.ok) {
      toast.notify('success', 'Job history cleared.')
      await refresh()
    }
  }, [clear, confirm, toast, refresh])

  const shown = showAll ? allJobs : allJobs.slice(0, PREVIEW)
  const last = jobs[0]
  const canLoadOlder = showAll && !exhausted && allJobs.length >= PAGE

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
          <span className="text-zinc-700">({allJobs.length})</span>
          {stale && <span className="text-red-500/70">· stale</span>}
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
          {last ? (
            <>
              last {last.status} <RelativeTime iso={last.created_at} />
            </>
          ) : (
            'no jobs yet'
          )}
        </p>
      ) : (
        <>
          <AdminActionFeedback actions={[trig, clear]} onKeyAccepted={() => void refresh()} />

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
                      {j.status === 'failed' && j.failure_category && (
                        <FailureBadge category={j.failure_category} summary={j.failure_summary} />
                      )}
                      <span className="font-mono text-zinc-300">{j.job_type}</span>
                      <span>
                        · <RelativeTime iso={j.created_at} />
                      </span>
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
                    {j.status === 'failed' && j.failure_summary && (
                      <p className="mt-1 text-zinc-500">{j.failure_summary}</p>
                    )}
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
              <div className="mt-2 flex items-center gap-3">
                {allJobs.length > PREVIEW && (
                  <button
                    type="button"
                    onClick={() => setShowAll((v) => !v)}
                    className="text-xs text-zinc-500 hover:text-zinc-300"
                  >
                    {showAll ? 'show less' : `show all (${allJobs.length})`}
                  </button>
                )}
                {canLoadOlder && (
                  <button
                    type="button"
                    onClick={() => void loadOlder()}
                    className="text-xs text-zinc-500 hover:text-zinc-300"
                  >
                    load older
                  </button>
                )}
              </div>
            </>
          )}
        </>
      )}
    </section>
  )
}
