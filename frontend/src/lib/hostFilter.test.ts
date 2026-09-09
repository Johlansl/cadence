import { describe, expect, it } from 'vitest'
import { EMPTY_FILTERS, filterHosts, filtersActive } from './hostFilter'
import type { HostSummary } from '../types'

function host(over: Partial<HostSummary> = {}): HostSummary {
  return {
    id: over.id ?? Math.random().toString(36).slice(2),
    hostname: 'vm-x',
    fqdn: null,
    description: null,
    os_family: 'debian',
    os_name: 'Debian',
    os_version: '12',
    package_manager: 'apt',
    agent_version: '0.6.0',
    reboot_required: false,
    reboot_policy: 'never',
    is_active: true,
    tags: {},
    last_seen_at: new Date().toISOString(),
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    status: 'up_to_date',
    updates_available_count: 0,
    security_updates_count: 0,
    excluded_count: 0,
    ...over,
  }
}

describe('filterHosts', () => {
  it('hides inactive hosts unless showInactive', () => {
    const hosts = [host({ hostname: 'a' }), host({ hostname: 'b', is_active: false })]
    expect(filterHosts(hosts, EMPTY_FILTERS).map((h) => h.hostname)).toEqual(['a'])
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, showInactive: true }).map((h) => h.hostname),
    ).toEqual(['a', 'b'])
  })

  it('text query matches hostname or description', () => {
    const hosts = [
      host({ hostname: 'vm-web', description: null }),
      host({ hostname: 'vm-db', description: 'primary' }),
    ]
    expect(filterHosts(hosts, { ...EMPTY_FILTERS, q: 'web' }).length).toBe(1)
    expect(filterHosts(hosts, { ...EMPTY_FILTERS, q: 'PRIMARY' }).map((h) => h.hostname)).toEqual([
      'vm-db',
    ])
  })

  it('"updates" status matches any host that is not up to date (security included)', () => {
    const hosts = [
      host({ hostname: 'ok', status: 'up_to_date' }),
      host({ hostname: 'upd', status: 'updates_available' }),
      host({ hostname: 'sec', status: 'security_updates_available' }),
    ]
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, status: 'updates' }).map((h) => h.hostname),
    ).toEqual(['upd', 'sec'])
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, status: 'security' }).map((h) => h.hostname),
    ).toEqual(['sec'])
  })

  it('tag query supports "key" and "key=value"', () => {
    const hosts = [
      host({ hostname: 'a', tags: { env: 'prod', role: 'web' } }),
      host({ hostname: 'b', tags: { env: 'lab' } }),
    ]
    expect(filterHosts(hosts, { ...EMPTY_FILTERS, tag: 'role' }).map((h) => h.hostname)).toEqual([
      'a',
    ])
    expect(filterHosts(hosts, { ...EMPTY_FILTERS, tag: 'env=lab' }).map((h) => h.hostname)).toEqual(
      ['b'],
    )
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, tag: 'env=prod' }).map((h) => h.hostname),
    ).toEqual(['a'])
  })

  it('overdue filter keeps only non-fresh hosts', () => {
    const old = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    const hosts = [host({ hostname: 'fresh' }), host({ hostname: 'stale', last_seen_at: old })]
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, freshness: 'silent' }).map((h) => h.hostname),
    ).toEqual(['stale'])
  })
})

describe('filtersActive', () => {
  it('is false for the empty filter and true once anything is set', () => {
    expect(filtersActive(EMPTY_FILTERS)).toBe(false)
    expect(filtersActive({ ...EMPTY_FILTERS, q: 'x' })).toBe(true)
    expect(filtersActive({ ...EMPTY_FILTERS, status: 'security' })).toBe(true)
    expect(filtersActive({ ...EMPTY_FILTERS, tag: 'env' })).toBe(true)
  })
})
