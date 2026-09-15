import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { installFetchMock, renderWithProviders } from './test/harness'
import type { HostSummary } from './types'

function hs(hostname: string, over: Partial<HostSummary> = {}): HostSummary {
  return {
    id: hostname,
    hostname,
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

const SUMMARY_ZERO = {
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
}

function installAppFetchMock() {
  return installFetchMock({
    'GET /api/v1/hosts': { body: [] },
    'GET /api/v1/fleet/summary': {
      body: {
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
      },
    },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
  window.history.replaceState(null, '', window.location.pathname)
})

describe('App enrollment navigation', () => {
  it('opens enrollment from the main navigation and writes a shareable hash', async () => {
    installAppFetchMock()
    renderWithProviders(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Enrollment' }))

    expect(screen.getByRole('heading', { name: 'Enrollment' })).toBeInTheDocument()
    expect(window.location.hash).toBe('#enrollment')
  })

  it('restores enrollment from the hash and returns to the fleet overview', async () => {
    window.history.replaceState(null, '', '#enrollment')
    installAppFetchMock()
    renderWithProviders(<App />)

    expect(screen.getByRole('heading', { name: 'Enrollment' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cadence' }))

    expect(await screen.findByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })
})

describe('App host-list loading / error', () => {
  it('shows loading in the main view before the first host fetch resolves', () => {
    installAppFetchMock()
    renderWithProviders(<App />)

    expect(screen.getAllByText('loading…').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { name: 'Fleet overview' })).not.toBeInTheDocument()
  })

  it('shows a main-view error distinct from the fleet when hosts fail to load', async () => {
    installFetchMock({ 'GET /api/v1/hosts': { status: 500, body: {} } })
    renderWithProviders(<App />)

    expect(await screen.findByText(/failed to load hosts/)).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Fleet overview' })).not.toBeInTheDocument()
  })
})

describe('App anomaly navigation', () => {
  const silent = () => new Date(Date.now() - 60 * 60 * 1000).toISOString()
  const late = () => new Date(Date.now() - 10 * 60 * 1000).toISOString()

  it('banner click lists late or silent hosts without selecting an arbitrary one', async () => {
    installFetchMock({
      'GET /api/v1/hosts': {
        body: [
          hs('vm-silent-1', { last_seen_at: silent() }),
          hs('vm-silent-2', { last_seen_at: silent() }),
          hs('vm-late-1', { last_seen_at: late() }),
          hs('vm-fresh-1'),
        ],
      },
      'GET /api/v1/fleet/summary': { body: SUMMARY_ZERO },
    })
    renderWithProviders(<App />)

    expect(await screen.findByText(/2 hosts silent/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /show late or silent/ }))

    // The existing freshness filter covers late hosts too: the sidebar shows
    // the whole set, the fresh host drops out, and no host gets selected.
    await waitFor(() => expect(screen.queryByText('vm-fresh-1')).not.toBeInTheDocument())
    expect(screen.getAllByText('vm-silent-1').length).toBeGreaterThan(0)
    expect(screen.getAllByText('vm-late-1').length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
  })

  it('keeps the selection across filters and reports the hidden count', async () => {
    installFetchMock({
      'GET /api/v1/hosts': { body: [hs('vm-1'), hs('vm-2'), hs('vm-3')] },
      'GET /api/v1/fleet/summary': { body: SUMMARY_ZERO },
    })
    renderWithProviders(<App />)
    await screen.findByText('vm-1')

    await userEvent.click(screen.getByRole('checkbox', { name: 'Select vm-1' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select vm-2' }))
    expect(screen.getByText(/2 selected/)).toBeInTheDocument()
    expect(screen.queryByText(/hidden/)).not.toBeInTheDocument()

    await userEvent.type(screen.getByLabelText(/filter hosts by name/i), 'vm-1')
    expect(await screen.findByText(/1 hidden by filters/)).toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText(/filter hosts by name/i))
    await waitFor(() => expect(screen.queryByText(/hidden/)).not.toBeInTheDocument())
    // The selection survived the filter round-trip.
    expect(screen.getByText(/2 selected/)).toBeInTheDocument()
    expect(screen.getByText('vm-2')).toBeInTheDocument()
  })

  it('+N more activates the needs-attention filter with the full set', async () => {
    const attention = Array.from({ length: 13 }, (_, i) =>
      hs(`vm-att-${String(i).padStart(2, '0')}`, { health_status: 'unhealthy' }),
    )
    installFetchMock({
      'GET /api/v1/hosts': { body: [...attention, hs('vm-fresh-1'), hs('vm-fresh-2')] },
      'GET /api/v1/fleet/summary': { body: SUMMARY_ZERO },
    })
    renderWithProviders(<App />)

    await userEvent.click(await screen.findByRole('button', { name: /more — show all 13/ }))

    await waitFor(() => expect(screen.queryByText('vm-fresh-1')).not.toBeInTheDocument())
    expect(screen.queryByText('vm-fresh-2')).not.toBeInTheDocument()
    expect(screen.getAllByText('vm-att-12').length).toBeGreaterThan(0)
  })
})
