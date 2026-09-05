export function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const delta = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (Math.abs(delta) < 5) return 'just now'
  const future = delta < 0
  const secs = Math.abs(delta)
  const at = (n: number, unit: string) => (future ? `in ${n}${unit}` : `${n}${unit} ago`)
  if (secs < 60) return at(secs, 's')
  const mins = Math.round(secs / 60)
  if (mins < 60) return at(mins, 'm')
  const hours = Math.round(mins / 60)
  if (hours < 48) return at(hours, 'h')
  return at(Math.round(hours / 24), 'd')
}

// Staleness buckets. The job-poll timer refreshes last_seen_at every minute
// (POST /api/v1/agent/next-job), so a healthy agent is always seconds old and a
// stopped one shows up within minutes. Hosts running only the hourly report
// (poll timer disabled) will read 'late'/'stale' -- that is expected.
export type Staleness = 'fresh' | 'late' | 'stale'

export function staleness(iso: string | null): Staleness {
  if (!iso) return 'stale'
  const mins = (Date.now() - new Date(iso).getTime()) / 60_000
  if (mins <= 5) return 'fresh'
  if (mins <= 15) return 'late'
  return 'stale'
}
