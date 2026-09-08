import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { pill } from '../lib/pill'
import type { PackageStatusFilter, PackageSummaryRow } from '../types'

const POLL_MS = 30_000
const DEBOUNCE_MS = 300
// Matches the backend default page size for GET /api/v1/packages. Sent
// explicitly so "is there another page" is just `page.length === PAGE_SIZE`.
const PAGE_SIZE = 50

const STATUS_OPTIONS: { value: PackageStatusFilter; label: string }[] = [
  { value: 'pending', label: 'pending updates' },
  { value: 'security', label: 'security only' },
  { value: 'all', label: 'all' },
]

// Answers "which hosts have <package> pending?" without opening each host.
export function PackagesView({ onSelectHost }: { onSelectHost: (id: string) => void }) {
  const [name, setName] = useState('')
  const [debouncedName, setDebouncedName] = useState('')
  const [status, setStatus] = useState<PackageStatusFilter>('pending')
  const [rows, setRows] = useState<PackageSummaryRow[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const t = setTimeout(() => setDebouncedName(name.trim()), DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [name])

  // First page: on mount, on any filter change, and on the poll. Replaces the
  // list (an expanded "Load more" view collapses back to page 1 on the poll).
  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const load = () =>
      api
        .listPackages({ name: debouncedName || undefined, status, limit: PAGE_SIZE })
        .then((r) => {
          if (cancelled) return
          setRows(r)
          setHasMore(r.length === PAGE_SIZE)
          setError(null)
        })
        .catch((e) => {
          if (!cancelled) setError(e instanceof Error ? e.message : String(e))
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    void load()
    const t = setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [debouncedName, status])

  const loadMore = () => {
    const last = rows[rows.length - 1]
    if (!last || loadingMore) return
    setLoadingMore(true)
    api
      .listPackages({
        name: debouncedName || undefined,
        status,
        limit: PAGE_SIZE,
        after: last.name,
        afterId: last.architecture,
      })
      .then((r) => {
        setRows((prev) => [...prev, ...r])
        setHasMore(r.length === PAGE_SIZE)
        setError(null)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoadingMore(false))
  }

  const emptyLabel =
    status === 'pending'
      ? 'No packages with a pending update.'
      : status === 'security'
        ? 'No packages with a pending security update.'
        : 'No packages match the filter.'

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm uppercase tracking-widest text-zinc-400">Packages</h2>

      <div className="flex flex-wrap items-center gap-3">
        <input
          type="search"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="filter by package name…"
          aria-label="Filter packages by name"
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
        />
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value as PackageStatusFilter)}
          aria-label="Filter by status"
          className="rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500"
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {error && <span className="text-xs text-red-400">sync error: {error}</span>}
      </div>

      {loading && rows.length === 0 ? (
        <p className="text-sm text-zinc-600">loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-zinc-600">{emptyLabel}</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((row) => (
            <li key={`${row.name}/${row.architecture}`} className="rounded border border-zinc-800">
              <div className="flex items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/40 px-3 py-1.5">
                <span className="font-mono text-sm text-zinc-200">
                  {row.name}
                  <span className="text-zinc-600">:{row.architecture}</span>
                </span>
                <span className="tabular-nums text-xs text-zinc-500">
                  {row.hosts.length} host{row.hosts.length === 1 ? '' : 's'}
                </span>
              </div>
              <ul className="divide-y divide-zinc-900 font-mono text-sm">
                {row.hosts.map((h) => (
                  <li
                    key={h.host_id}
                    className="flex items-center justify-between gap-3 px-3 py-1.5"
                  >
                    <button
                      type="button"
                      onClick={() => onSelectHost(h.host_id)}
                      className="truncate text-zinc-200 hover:text-zinc-100 hover:underline"
                    >
                      {h.hostname}
                    </button>
                    <span className="flex shrink-0 items-center gap-2 text-xs text-zinc-500">
                      <span>
                        {h.installed_version}
                        {h.candidate_version && (
                          <>
                            {' → '}
                            <span className="text-zinc-300">{h.candidate_version}</span>
                          </>
                        )}
                      </span>
                      {h.is_security_update && <span className={pill('danger')}>SEC</span>}
                      {h.advisories.map((a) => (
                        <a
                          key={a.id}
                          href={a.url}
                          target="_blank"
                          rel="noreferrer"
                          title={a.cves.join(', ')}
                          className={`${pill('info')} hover:text-sky-300`}
                        >
                          {a.id}
                        </a>
                      ))}
                    </span>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}

      {hasMore && (
        <button
          type="button"
          onClick={loadMore}
          disabled={loadingMore}
          className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:border-zinc-500 disabled:opacity-50"
        >
          {loadingMore ? 'loading…' : 'Load more'}
        </button>
      )}
    </div>
  )
}
