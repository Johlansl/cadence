import type { HostSummary } from '../types'
import { Freshness } from './Freshness'
import { StatusBadge } from './StatusBadge'
import { TagChips } from './TagChips'

interface Props {
  hosts: HostSummary[]
  selectedId: string | null
  onSelect: (id: string) => void
  emptyLabel?: string
}

export function HostList({ hosts, selectedId, onSelect, emptyLabel = 'No hosts.' }: Props) {
  if (hosts.length === 0) {
    return <p className="p-4 text-sm text-zinc-500">{emptyLabel}</p>
  }

  // Active hosts first, then inactive; alphabetical within each group.
  const ordered = [...hosts].sort(
    (a, b) => Number(b.is_active) - Number(a.is_active) || a.hostname.localeCompare(b.hostname),
  )

  return (
    <ul className="divide-y divide-zinc-800">
      {ordered.map((h) => {
        const active = h.id === selectedId
        const normalUpdates = h.updates_available_count - h.security_updates_count
        return (
          <li key={h.id}>
            <button
              type="button"
              onClick={() => onSelect(h.id)}
              aria-current={active ? 'true' : undefined}
              className={`flex w-full flex-col gap-1 px-4 py-3 text-left transition-colors ${
                active ? 'bg-zinc-800/80' : 'hover:bg-zinc-900'
              } ${h.is_active ? '' : 'opacity-40'}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-sm text-zinc-100">{h.hostname}</span>
                <StatusBadge status={h.status} />
              </div>

              <div className="flex items-center justify-between gap-2 text-xs text-zinc-500">
                <span className="truncate">
                  {(h.os_name ?? h.os_family) + (h.os_version ? ` ${h.os_version}` : '')}
                </span>
                <span className="shrink-0 space-x-1">
                  {h.security_updates_count > 0 && (
                    <span className="text-red-400">{h.security_updates_count} sec</span>
                  )}
                  {normalUpdates > 0 && <span className="text-amber-400">{normalUpdates} upd</span>}
                </span>
              </div>

              <div className="flex items-center gap-2 text-xs text-zinc-600">
                <span>
                  seen <Freshness iso={h.last_seen_at} />
                </span>
                {h.reboot_required && <span className="text-orange-400">· reboot</span>}
              </div>

              {Object.keys(h.tags).length > 0 && (
                <div className="mt-0.5">
                  <TagChips tags={h.tags} />
                </div>
              )}
            </button>
          </li>
        )
      })}
    </ul>
  )
}
