import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ReportSummary } from '../types'
import { RelativeTime } from './RelativeTime'
import { Sparkline } from './Sparkline'

export function HostHistory({ hostId }: { hostId: string }) {
  const [reports, setReports] = useState<ReportSummary[] | null>(null)
  const [stale, setStale] = useState(false)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .getHostReports(hostId, { limit: 60 })
        .then((r) => {
          if (!cancelled) {
            setReports(r)
            setStale(false)
          }
        })
        .catch(() => !cancelled && setStale(true))
    void load()
    const t = setInterval(load, 60_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [hostId])

  // API returns newest-first; chart oldest-to-newest.
  const chrono = reports ? [...reports].reverse() : []

  return (
    <section className="border-t border-zinc-800 px-6 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wide text-zinc-600">
          History
          {stale && <span className="ml-1.5 normal-case text-red-500/70">· stale</span>}
        </h3>
        {reports && (
          <span className="text-xs text-zinc-600">
            {reports.length} report{reports.length === 1 ? '' : 's'}
            {chrono.length > 0 && (
              <>
                {' · since '}
                <RelativeTime iso={chrono[0].received_at} />
              </>
            )}
          </span>
        )}
      </div>

      {chrono.length < 2 ? (
        <p className="mt-2 text-xs text-zinc-600">Not enough history yet.</p>
      ) : (
        <div className="mt-2 flex flex-wrap items-center gap-4">
          <Sparkline
            width={280}
            height={44}
            series={[
              { values: chrono.map((r) => r.updates_available_count), stroke: 'text-amber-400' },
              { values: chrono.map((r) => r.security_updates_count), stroke: 'text-red-400' },
            ]}
          />
          <dl className="flex gap-4 text-xs">
            <div>
              <dt className="text-amber-400">updates</dt>
              <dd className="font-mono text-zinc-300">
                {chrono[chrono.length - 1].updates_available_count}
              </dd>
            </div>
            <div>
              <dt className="text-red-400">security</dt>
              <dd className="font-mono text-zinc-300">
                {chrono[chrono.length - 1].security_updates_count}
              </dd>
            </div>
            <div>
              <dt className="text-zinc-500">installed</dt>
              <dd className="font-mono text-zinc-300">
                {chrono[chrono.length - 1].installed_package_count}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </section>
  )
}
