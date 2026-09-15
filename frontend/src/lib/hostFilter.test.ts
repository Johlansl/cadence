import { describe, expect, it } from 'vitest'
import {
  attentionRank,
  EMPTY_FILTERS,
  filterHosts,
  filtersActive,
  needsAttention,
} from './hostFilter'
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
    health_status: 'unknown',
    health_checked_at: null,
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

  it('freshness filter keeps late and silent hosts, drops fresh ones', () => {
    const late = new Date(Date.now() - 10 * 60 * 1000).toISOString()
    const silent = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    const hosts = [
      host({ hostname: 'fresh' }),
      host({ hostname: 'late', last_seen_at: late }),
      host({ hostname: 'silent', last_seen_at: silent }),
    ]
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, freshness: 'silent' }).map((h) => h.hostname),
    ).toEqual(['late', 'silent'])
  })

  it('attention filter keeps only hosts matching the fleet needs-attention definition', () => {
    const hosts = [
      host({ hostname: 'ok', health_status: 'healthy' }),
      host({ hostname: 'unhealthy', health_status: 'unhealthy' }),
      host({ hostname: 'sec', health_status: 'healthy', status: 'security_updates_available' }),
      host({ hostname: 'reboot', health_status: 'healthy', reboot_required: true }),
      host({
        hostname: 'inactive-unhealthy',
        health_status: 'unhealthy',
        is_active: false,
      }),
    ]
    expect(
      filterHosts(hosts, { ...EMPTY_FILTERS, attention: true }).map((h) => h.hostname),
    ).toEqual(['unhealthy', 'sec', 'reboot'])
  })
})

describe('attentionRank', () => {
  it('orders unhealthy before security before degraded before reboot before updates before unknown before stale-only', () => {
    const old = new Date(Date.now() - 60 * 60 * 1000).toISOString()
    const rank = (over: Partial<HostSummary>) =>
      attentionRank(host({ health_status: 'healthy', ...over }))
    const ranks = [
      rank({ health_status: 'unhealthy' }),
      rank({ status: 'security_updates_available' }),
      rank({ health_status: 'degraded' }),
      rank({ reboot_required: true }),
      rank({ status: 'updates_available' }),
      rank({ health_status: 'unknown' }),
      rank({ last_seen_at: old }),
    ]
    expect([...ranks].sort((a, b) => a - b)).toEqual(ranks)
    expect(attentionRank(host({ health_status: 'healthy' }))).toBe(99)
  })

  it('needsAttention matches the fleet filter: active and ranked', () => {
    expect(needsAttention(host({ health_status: 'unhealthy' }))).toBe(true)
    expect(needsAttention(host({ health_status: 'healthy' }))).toBe(false)
    expect(needsAttention(host({ health_status: 'unhealthy', is_active: false }))).toBe(false)
  })
})

describe('filtersActive', () => {
  it('is false for the empty filter and true once anything is set', () => {
    expect(filtersActive(EMPTY_FILTERS)).toBe(false)
    expect(filtersActive({ ...EMPTY_FILTERS, q: 'x' })).toBe(true)
    expect(filtersActive({ ...EMPTY_FILTERS, status: 'security' })).toBe(true)
    expect(filtersActive({ ...EMPTY_FILTERS, tag: 'env' })).toBe(true)
    expect(filtersActive({ ...EMPTY_FILTERS, attention: true })).toBe(true)
  })
})
