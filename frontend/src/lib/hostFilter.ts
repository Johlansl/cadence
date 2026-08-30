import { staleness } from './time'
import type { HostSummary } from '../types'

export type StatusFilter = 'all' | 'security' | 'updates' | 'uptodate'
export type FreshnessFilter = 'all' | 'silent'

export interface HostFilters {
  q: string
  status: StatusFilter
  freshness: FreshnessFilter
  showInactive: boolean
}

export const EMPTY_FILTERS: HostFilters = {
  q: '',
  status: 'all',
  freshness: 'all',
  showInactive: false,
}

export function filtersActive(f: HostFilters): boolean {
  return (
    f.q.trim() !== '' ||
    f.status !== 'all' ||
    f.freshness !== 'all' ||
    f.showInactive
  )
}

function matchesStatus(h: HostSummary, s: StatusFilter): boolean {
  switch (s) {
    case 'security':
      return h.status === 'security_updates_available'
    case 'updates':
      return h.status === 'updates_available'
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
    return true
  })
}
