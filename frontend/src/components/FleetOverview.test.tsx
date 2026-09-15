import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock } from '../test/harness'
import type { FleetSummary, HostSummary } from '../types'
import { FleetOverview } from './FleetOverview'

function host(over: Partial<HostSummary> = {}): HostSummary {
  return {
    id: over.id ?? over.hostname ?? Math.random().toString(36).slice(2),
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
    health_status: 'healthy',
    health_checked_at: null,
    ...over,
  }
}

function summary(over: Partial<FleetSummary> = {}): FleetSummary {
  return {
    total_hosts: 0,
    active_hosts: 0,
    inactive_hosts: 0,
    up_to_date: 0,
    updates_available: 0,
    security_updates_available: 0,
    reboot_required: 0,
    late: 0,
    silent: 0,
    pending_updates: 0,
    security_updates: 0,
    oldest_report_age_seconds: null,
    jobs_running: 0,
    jobs_succeeded_24h: 0,
    jobs_failed_24h: 0,
    ...over,
  }
}

const URL = 'GET /api/v1/fleet/summary'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('FleetOverview summary states', () => {
  it('shows a pending marker, not zero or n/a, while the first fetch is in flight', () => {
    installFetchMock({ [URL]: { body: summary() } })
    render(<FleetOverview hosts={[]} onSelect={() => {}} onShowAttention={() => {}} />)

    // Security, Needs updates, Reboot, Overdue, Jobs 24h are summary-driven.
    expect(screen.getAllByText('…')).toHaveLength(5)
    expect(screen.queryByText('n/a')).not.toBeInTheDocument()
    expect(screen.queryByText(/sync error/)).not.toBeInTheDocument()
  })

  it('shows real zeros once a successful fetch returns zero', async () => {
    installFetchMock({ [URL]: { body: summary() } })
    render(<FleetOverview hosts={[]} onSelect={() => {}} onShowAttention={() => {}} />)

    await screen.findByText('Fleet overview')
    // Hosts, Security, Health, Needs updates, Reboot, Overdue all read 0.
    expect(screen.getAllByText('0').length).toBeGreaterThanOrEqual(5)
    expect(screen.queryByText('n/a')).not.toBeInTheDocument()
    expect(screen.queryByText('…')).not.toBeInTheDocument()
    expect(screen.queryByText(/sync error/)).not.toBeInTheDocument()
  })

  it('shows n/a plus an error, never zero, when the summary cannot be loaded', async () => {
    installFetchMock({ [URL]: { status: 500, body: {} } })
    render(<FleetOverview hosts={[]} onSelect={() => {}} onShowAttention={() => {}} />)

    expect(await screen.findByText(/sync error/)).toBeInTheDocument()
    expect(screen.getAllByText('n/a')).toHaveLength(5)
    expect(screen.queryByText('…')).not.toBeInTheDocument()
  })

  it('keeps the previous values and marks them stale when a refresh fails', async () => {
    vi.useFakeTimers()
    try {
      let fail = false
      installFetchMock({
        [URL]: () =>
          fail
            ? { status: 500, body: {} }
            : {
                body: summary({
                  security_updates_available: 3,
                  jobs_succeeded_24h: 5,
                  jobs_failed_24h: 1,
                }),
              },
      })
      render(<FleetOverview hosts={[]} onSelect={() => {}} onShowAttention={() => {}} />)
      await act(async () => {
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      expect(screen.getByText('5✓ 1✕')).toBeInTheDocument()

      fail = true
      await act(async () => {
        vi.advanceTimersByTime(30_000)
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      // Previous values stay on screen, flagged as stale: never back to
      // loading, unavailable, or empty.
      expect(screen.getByText('5✓ 1✕')).toBeInTheDocument()
      expect(screen.getByText(/stale/)).toBeInTheDocument()
      expect(screen.queryByText('n/a')).not.toBeInTheDocument()
      expect(screen.queryByText('…')).not.toBeInTheDocument()
      expect(screen.queryByText(/sync error/)).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('renders 12 attention hosts plus an action delegating the rest', async () => {
    installFetchMock({ [URL]: { body: summary() } })
    const onShowAttention = vi.fn()
    const hosts = Array.from({ length: 13 }, (_, i) =>
      host({ hostname: `vm-att-${String(i).padStart(2, '0')}`, health_status: 'unhealthy' }),
    )
    render(<FleetOverview hosts={hosts} onSelect={() => {}} onShowAttention={onShowAttention} />)

    expect(await screen.findByText('Needs attention (13)')).toBeInTheDocument()
    expect(screen.getByText('vm-att-00')).toBeInTheDocument()
    expect(screen.getByText('vm-att-11')).toBeInTheDocument()
    expect(screen.queryByText('vm-att-12')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /more — show all 13/ }))
    expect(onShowAttention).toHaveBeenCalledTimes(1)
  })
})
