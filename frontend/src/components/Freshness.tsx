import { relativeTime, staleness, type Staleness } from '../lib/time'

const COLOR: Record<Staleness, string> = {
  fresh: 'text-emerald-400',
  late: 'text-amber-400',
  stale: 'text-red-400',
}

// Relative "last seen" time, coloured by how overdue the host is:
// green ≤ 5 min, amber ≤ 15 min, red beyond (see staleness()).
export function Freshness({ iso }: { iso: string | null }) {
  return (
    <span className={COLOR[staleness(iso)]} title={iso ?? 'no report received yet'}>
      {relativeTime(iso)}
    </span>
  )
}
