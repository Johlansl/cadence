import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SilentBanner, summarize } from './Overview'
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

const stale = () => new Date(Date.now() - 60 * 60 * 1000).toISOString()

describe('summarize', () => {
  it('partitions active hosts by worst status and counts silent', () => {
    const c = summarize([
      host({ status: 'security_updates_available' }),
      host({ status: 'updates_available' }),
      host({ status: 'up_to_date' }),
      host({ status: 'up_to_date', last_seen_at: stale() }),
      host({ is_active: false, status: 'security_updates_available' }),
    ])
    expect(c.total).toBe(4)
    expect(c.security).toBe(1)
    expect(c.updates).toBe(1)
    expect(c.silent).toBe(1)
  })

  it('counts current health for active hosts only', () => {
    const c = summarize([
      host({ health_status: 'unhealthy' }),
      host({ health_status: 'degraded' }),
      host({ health_status: 'unknown' }),
      host({ is_active: false, health_status: 'unhealthy' }),
    ])
    expect(c.unhealthy).toBe(1)
    expect(c.degraded).toBe(1)
    expect(c.unknownHealth).toBe(1)
  })
})

describe('SilentBanner', () => {
  it('renders nothing when every active host is fresh', () => {
    const { container } = render(<SilentBanner hosts={[host()]} onSelect={() => {}} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('shows silent hosts and selects the first on click', async () => {
    const onSelect = vi.fn()
    render(
      <SilentBanner
        hosts={[host({ id: 's1', hostname: 'vm-silent', last_seen_at: stale() })]}
        onSelect={onSelect}
      />,
    )
    expect(screen.getByText(/host.*silent/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button'))
    expect(onSelect).toHaveBeenCalledWith('s1')
  })
})
