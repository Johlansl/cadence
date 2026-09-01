import { staleness } from './time'
import type { HostSummary } from '../types'

export type StatusFilter = 'all' | 'security' | 'updates' | 'uptodate'
export type FreshnessFilter = 'all' | 'silent'

export interface HostFilters {
  q: string
  status: StatusFilter
  freshness: FreshnessFilter
  showInactive: boolean
  tag: string
}

export const EMPTY_FILTERS: HostFilters = {
  q: '',
  status: 'all',
  freshness: 'all',
  showInactive: false,
  tag: '',
}

export function filtersActive(f: HostFilters): boolean {
  return (
    f.q.trim() !== '' ||
    f.status !== 'all' ||
    f.freshness !== 'all' ||
    f.showInactive ||
    f.tag.trim() !== ''
  )
}

// A tag query is either "key" (host has that key) or "key=value" (exact pair).
// Matching is case-insensitive; the key part also matches as a substring.
function matchesTag(tags: Record<string, string>, query: string): boolean {
  const q = query.trim().toLowerCase()
  if (!q) return true
  const [k, v] = q.split('=', 2)
  return Object.entries(tags).some(([key, value]) => {
    const keyLc = key.toLowerCase()
    if (v === undefined) return keyLc.includes(k) || value.toLowerCase().includes(k)
    return keyLc === k && value.toLowerCase() === v
  })
}

function matchesStatus(h: HostSummary, s: StatusFilter): boolean {
  switch (s) {
    case 'security':
      return h.status === 'security_updates_available'
    // "updates" means "has any pending updates" -- a host with security updates
    // has updates too, so it matches here as well.
    case 'updates':
      return h.status !== 'up_to_date'
    case 'uptodate':
      return h.status === 'up_to_date'
    default:
      return true
  }
}

export function filterHosts(hosts: HostSummary[], f: HostFilters): HostSummary[] {
  const q = f.q.trim().toLowerCase()
  return hosts.filter((h) => {
    if (!f.showInactive && !h.is_active) return false
    if (q && !`${h.hostname} ${h.description ?? ''}`.toLowerCase().includes(q)) return false
    if (!matchesStatus(h, f.status)) return false
    if (f.freshness === 'silent' && staleness(h.last_seen_at) === 'fresh') return false
    if (!matchesTag(h.tags, f.tag)) return false
    return true
  })
}
