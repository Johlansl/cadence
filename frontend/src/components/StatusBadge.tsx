import type { HostStatus } from '../types'

const STYLES: Record<HostStatus, { label: string; cls: string }> = {
  up_to_date: {
    label: 'up to date',
    cls: 'bg-emerald-500/10 text-emerald-400 ring-emerald-500/30',
  },
  updates_available: {
    label: 'updates',
    cls: 'bg-amber-500/10 text-amber-400 ring-amber-500/30',
  },
  security_updates_available: {
    label: 'security',
    cls: 'bg-red-500/10 text-red-400 ring-red-500/30',
  },
}

export function StatusBadge({ status }: { status: HostStatus }) {
  const s = STYLES[status]
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ring-1 ${s.cls}`}
    >
      {s.label}
    </span>
  )
}
