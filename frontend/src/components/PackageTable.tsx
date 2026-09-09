import { useMemo, useState } from 'react'
import type { HostPackage } from '../types'

// security update first, then any update, then the rest, name as tie-breaker.
function smartRank(p: HostPackage): number {
  if (p.candidate_version && p.is_security_update) return 0
  if (p.candidate_version) return 1
  return 2
}

type SortKey = 'smart' | 'name' | 'installed' | 'candidate' | 'origin'
type SortDir = 'asc' | 'desc'

const FIELD: Record<Exclude<SortKey, 'smart'>, (p: HostPackage) => string> = {
  name: (p) => p.name,
  installed: (p) => p.installed_version,
  candidate: (p) => p.candidate_version ?? '',
  origin: (p) => p.update_origin ?? '',
}

const COLUMNS: { key: Exclude<SortKey, 'smart'>; label: string; cls: string }[] = [
  { key: 'name', label: 'Package', cls: 'px-6' },
  { key: 'installed', label: 'Installed', cls: 'px-3' },
  { key: 'candidate', label: 'Candidate', cls: 'px-3' },
  { key: 'origin', label: 'Origin', cls: 'px-3' },
]

export function PackageTable({ packages }: { packages: HostPackage[] }) {
  const [q, setQ] = useState('')
  const [onlyUpdates, setOnlyUpdates] = useState(true)
  const [onlySecurity, setOnlySecurity] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('smart')
  const [sortDir, setSortDir] = useState<SortDir>('asc')

  const clickColumn = (key: Exclude<SortKey, 'smart'>) => {
    if (sortKey !== key) {
      setSortKey(key)
      setSortDir('asc')
    } else if (sortDir === 'asc') {
      setSortDir('desc')
    } else {
      setSortKey('smart') // third click restores the priority order
    }
  }

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase()
    let out = packages.filter((p) => {
      if (onlyUpdates && !p.candidate_version) return false
      if (onlySecurity && !p.is_security_update) return false
      if (needle) {
        const advisoryText = p.advisories.map((a) => `${a.id} ${a.cves.join(' ')}`).join(' ')
        const haystack = `${p.name} ${p.update_origin ?? ''} ${advisoryText}`.toLowerCase()
        if (!haystack.includes(needle)) return false
      }
      return true
    })
    if (sortKey === 'smart') {
      out = out.sort((a, b) => smartRank(a) - smartRank(b) || a.name.localeCompare(b.name))
    } else {
      const get = FIELD[sortKey]
      const s = sortDir === 'asc' ? 1 : -1
      out = out.sort((a, b) => s * get(a).localeCompare(get(b)) || a.name.localeCompare(b.name))
    }
    return out
  }, [packages, q, onlyUpdates, onlySecurity, sortKey, sortDir])

  const indicator = (key: string) => (sortKey === key ? (sortDir === 'asc' ? ' ▲' : ' ▼') : '')

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-zinc-800 px-6 py-2 text-xs text-zinc-500">
        <span className="tabular-nums">
          {rows.length === packages.length
            ? `${packages.length} package${packages.length === 1 ? '' : 's'}`
            : `${rows.length} of ${packages.length} packages`}
        </span>
        <div className="flex flex-wrap items-center gap-3">
          <input
            type="search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="filter packages…"
            aria-label="Filter packages by name or origin"
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-0.5 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
          />
          <label className="flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              checked={onlySecurity}
              onChange={(e) => setOnlySecurity(e.target.checked)}
              className="accent-zinc-400"
            />
            security only
          </label>
          <label className="flex cursor-pointer items-center gap-1.5 select-none">
            <input
              type="checkbox"
              checked={onlyUpdates}
              onChange={(e) => setOnlyUpdates(e.target.checked)}
              className="accent-zinc-400"
            />
            only pending updates
          </label>
        </div>
      </div>

      <table className="w-full text-left text-sm">
        <thead className="sticky top-0 bg-zinc-950 text-xs uppercase tracking-wide text-zinc-600">
          <tr>
            {COLUMNS.map((c) => (
              <th key={c.key} className={`${c.cls} py-2 font-medium`}>
                <button
                  type="button"
                  onClick={() => clickColumn(c.key)}
                  className="uppercase tracking-wide hover:text-zinc-300"
                  aria-sort={
                    sortKey === c.key ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'
                  }
                >
                  {c.label}
                  {indicator(c.key)}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-900 font-mono">
          {rows.map((p) => (
            <tr key={`${p.name}/${p.architecture}`} className="hover:bg-zinc-900/50">
              <td className="px-6 py-1.5 text-zinc-200">
                {p.name}
                <span className="text-zinc-600">:{p.architecture}</span>
                {p.is_security_update && (
                  <span className="ml-2 rounded bg-red-500/10 px-1 font-sans text-[10px] font-medium text-red-400 ring-1 ring-red-500/30">
                    SEC
                  </span>
                )}
                {p.excluded && (
                  <span
                    className="ml-2 rounded bg-zinc-700/40 px-1 font-sans text-[10px] font-medium text-zinc-400 ring-1 ring-zinc-600"
                    title="excluded by policy: never auto-upgraded"
                  >
                    HELD
                  </span>
                )}
                {p.advisories.map((a) => (
                  <a
                    key={a.id}
                    href={a.url}
                    target="_blank"
                    rel="noreferrer"
                    title={a.cves.join(', ')}
                    className="ml-1 rounded bg-sky-500/10 px-1 font-sans text-[10px] font-medium text-sky-400 ring-1 ring-sky-500/30 hover:text-sky-300"
                  >
                    {a.id}
                  </a>
                ))}
              </td>
              <td className="px-3 py-1.5 text-zinc-500">{p.installed_version}</td>
              <td className="px-3 py-1.5 text-zinc-300">{p.candidate_version ?? '-'}</td>
              <td className="px-3 py-1.5 text-zinc-600">{p.update_origin ?? '-'}</td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={4} className="px-6 py-6 text-center font-sans text-zinc-600">
                {packages.length === 0
                  ? 'No packages reported.'
                  : onlyUpdates && !onlySecurity && !q.trim()
                    ? 'No pending updates.'
                    : 'No packages match the filter.'}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  )
}
