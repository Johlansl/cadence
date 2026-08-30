import type { HostFilters, StatusFilter } from '../lib/hostFilter'
import { EMPTY_FILTERS, filtersActive } from '../lib/hostFilter'

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: 'all' },
  { value: 'security', label: 'security' },
  { value: 'updates', label: 'updates' },
  { value: 'uptodate', label: 'up to date' },
]

const selectCls =
  'rounded border border-zinc-700 bg-zinc-900 px-1.5 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'

export function HostFilters({
  value,
  onChange,
  shown,
  total,
}: {
  value: HostFilters
  onChange: (next: HostFilters) => void
  shown: number
  total: number
}) {
  const set = <K extends keyof HostFilters>(k: K, v: HostFilters[K]) =>
    onChange({ ...value, [k]: v })

  return (
    <div className="space-y-2 border-b border-zinc-800 px-3 py-2">
      <input
        type="search"
        value={value.q}
        onChange={(e) => set('q', e.target.value)}
        placeholder="filter hosts…"
        aria-label="Filter hosts by name or description"
        className="w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs text-zinc-200 outline-none focus:border-zinc-500"
      />
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-zinc-500">
        <select
          value={value.status}
          onChange={(e) => set('status', e.target.value as StatusFilter)}
          aria-label="Filter by status"
          className={selectCls}
        >
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <label className="flex items-center gap-1 select-none">
          <input
            type="checkbox"
            checked={value.freshness === 'silent'}
            onChange={(e) => set('freshness', e.target.checked ? 'silent' : 'all')}
            className="accent-zinc-400"
          />
          overdue
        </label>
        <label className="flex items-center gap-1 select-none">
          <input
            type="checkbox"
            checked={value.showInactive}
            onChange={(e) => set('showInactive', e.target.checked)}
            className="accent-zinc-400"
          />
          inactive
        </label>
        {filtersActive(value) && (
          <button
            type="button"
            onClick={() => onChange(EMPTY_FILTERS)}
            className="text-zinc-600 hover:text-zinc-300"
          >
            clear
          </button>
        )}
        <span className="ml-auto tabular-nums text-zinc-600">
          {shown === total ? total : `${shown}/${total}`}
        </span>
      </div>
    </div>
  )
}
