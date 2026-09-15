import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock } from '../test/harness'
import type { ReportSummary } from '../types'
import { HostHistory } from './HostHistory'

let seq = 0
function report(over: Partial<ReportSummary> = {}): ReportSummary {
  return {
    id: ++seq,
    received_at: '2026-08-01T00:00:00Z',
    agent_version: '0.6.0',
    installed_package_count: 100,
    updates_available_count: 3,
    security_updates_count: 1,
    reboot_required: false,
    ...over,
  }
}

const URL = 'GET /api/v1/hosts/h1/reports'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('HostHistory loading / empty / stale', () => {
  it('shows loading on the first fetch, not the insufficient-history state', () => {
    installFetchMock({ [URL]: { body: [] } })
    render(<HostHistory hostId="h1" />)

    expect(screen.getByText('loading…')).toBeInTheDocument()
    expect(screen.queryByText('Not enough history yet.')).not.toBeInTheDocument()
  })

  it('shows an error distinct from insufficient history when the first fetch fails', async () => {
    installFetchMock({ [URL]: { status: 500, body: {} } })
    render(<HostHistory hostId="h1" />)

    expect(await screen.findByText("couldn't load history.")).toBeInTheDocument()
    expect(screen.queryByText('Not enough history yet.')).not.toBeInTheDocument()
  })

  it('shows insufficient history only after a successful fetch returns too few reports', async () => {
    installFetchMock({ [URL]: { body: [report()] } })
    render(<HostHistory hostId="h1" />)

    expect(await screen.findByText('Not enough history yet.')).toBeInTheDocument()
    expect(screen.queryByText("couldn't load history.")).not.toBeInTheDocument()
  })

  it('shows the history once enough reports arrive', async () => {
    installFetchMock({ [URL]: { body: [report(), report()] } })
    render(<HostHistory hostId="h1" />)

    expect(await screen.findByText(/2 reports/)).toBeInTheDocument()
    expect(screen.queryByText('Not enough history yet.')).not.toBeInTheDocument()
  })

  it('keeps the loaded history and marks it stale when a refresh fails', async () => {
    vi.useFakeTimers()
    try {
      let fail = false
      installFetchMock({
        [URL]: () => (fail ? { status: 500, body: {} } : { body: [report(), report()] }),
      })
      render(<HostHistory hostId="h1" />)
      await act(async () => {
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      expect(screen.getByText(/2 reports/)).toBeInTheDocument()

      fail = true
      await act(async () => {
        vi.advanceTimersByTime(60_000)
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      // Previous data stays on screen, flagged as stale instead of vanishing.
      expect(screen.getByText(/2 reports/)).toBeInTheDocument()
      expect(screen.getByText(/stale/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
