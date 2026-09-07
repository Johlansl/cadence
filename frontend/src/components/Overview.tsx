import { staleness } from '../lib/time'
import { useNow } from '../lib/useNow'
import type { HostSummary } from '../types'

interface Counts {
  total: number
  upToDate: number
  updates: number
  security: number
  reboot: number
  silent: number // no report in > 15 min (staleness 'stale')
  late: number // 5-15 min
}

// Fleet health, computed from the host list. Inactive hosts are ignored.
export function summarize(hosts: HostSummary[]): Counts {
  const active = hosts.filter((h) => h.is_active)
  const c: Counts = {
    total: active.length,
    upToDate: 0,
    updates: 0,
    security: 0,
    reboot: 0,
    silent: 0,
    late: 0,
  }
  for (const h of active) {
    if (h.status === 'security_updates_available') c.security++
    else if (h.status === 'updates_available') c.updates++
    else c.upToDate++
    if (h.reboot_required) c.reboot++
    const s = staleness(h.last_seen_at)
    if (s === 'stale') c.silent++
    else if (s === 'late') c.late++
  }
  return c
}

export function OverviewChips({ hosts }: { hosts: HostSummary[] }) {
  useNow()
  const c = summarize(hosts)
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-zinc-600">
      <span>
        {c.total} host{c.total === 1 ? '' : 's'}
      </span>
      {c.security > 0 && <span className="text-red-400">{c.security} security</span>}
      {c.updates > 0 && <span className="text-amber-400">{c.updates} updates</span>}
      {c.upToDate > 0 && <span className="text-emerald-500">{c.upToDate} up to date</span>}
      {c.reboot > 0 && <span className="text-orange-400">{c.reboot} reboot</span>}
      {c.silent > 0 && <span className="text-red-400">{c.silent} silent</span>}
      {c.late > 0 && <span className="text-amber-400">{c.late} late</span>}
    </div>
  )
}

export function SilentBanner({
  hosts,
  onSelect,
}: {
  hosts: HostSummary[]
  onSelect: (id: string) => void
}) {
  useNow()
  const silent = hosts.filter((h) => h.is_active && staleness(h.last_seen_at) === 'stale')
  if (silent.length === 0) return null

  const shown = silent
    .slice(0, 3)
    .map((h) => h.hostname)
    .join(', ')
  const extra = silent.length > 3 ? ` +${silent.length - 3}` : ''

  return (
    <button
      type="button"
      onClick={() => onSelect(silent[0].id)}
      className="w-full border-b border-red-500/30 bg-red-500/10 px-6 py-2 text-left text-xs text-red-300 hover:bg-red-500/15"
    >
      <span className="font-medium">
        {silent.length} host{silent.length === 1 ? '' : 's'} silent
      </span>{' '}
      with no report in over 15 min:{' '}
      <span className="font-mono">
        {shown}
        {extra}
      </span>
    </button>
  )
}
