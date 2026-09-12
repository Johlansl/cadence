import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { installFetchMock, renderWithProviders } from './test/harness'

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

    expect(screen.getByRole('heading', { name: 'Fleet overview' })).toBeInTheDocument()
    expect(window.location.hash).toBe('')
  })
})
