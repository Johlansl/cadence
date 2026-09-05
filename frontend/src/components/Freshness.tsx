import { TONE_TEXT } from '../lib/pill'
import { relativeTime, staleness, type Staleness } from '../lib/time'
import { useNow } from '../lib/useNow'

const COLOR: Record<Staleness, string> = {
  fresh: TONE_TEXT.ok,
  late: TONE_TEXT.warn,
  stale: TONE_TEXT.danger,
}

// Relative "last seen" time, coloured by how overdue the host is:
// green ≤ 5 min, amber ≤ 15 min, red beyond (see staleness()). Self-refreshing
// so the label and the colour cross the thresholds on their own.
export function Freshness({ iso }: { iso: string | null }) {
  useNow()
  return (
    <span className={COLOR[staleness(iso)]} title={iso ?? 'no report received yet'}>
      {relativeTime(iso)}
    </span>
  )
}
