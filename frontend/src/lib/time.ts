export function relativeTime(iso: string | null): string {
  if (!iso) return 'never'
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (secs < 5) return 'just now'
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  if (mins < 60) return `${mins}m ago`
  const hours = Math.round(mins / 60)
  if (hours < 48) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

// Staleness buckets for an agent expected to report roughly hourly.
export type Staleness = 'fresh' | 'late' | 'stale'

export function staleness(iso: string | null): Staleness {
  if (!iso) return 'stale'
  const mins = (Date.now() - new Date(iso).getTime()) / 60_000
  if (mins <= 90) return 'fresh'
  if (mins <= 360) return 'late'
  return 'stale'
}
