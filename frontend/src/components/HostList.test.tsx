import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { HostSummary } from '../types'
import { HostList } from './HostList'

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

const old = () => new Date(Date.now() - 60 * 60 * 1000).toISOString()

function renderList(hosts: HostSummary[], attentionOrder = false) {
  render(
    <HostList
      hosts={hosts}
      selectedId={null}
      onSelect={() => {}}
      checkedIds={new Set()}
      onToggleCheck={() => {}}
      attentionOrder={attentionOrder}
    />,
  )
}

function buttonOrder(): string {
  return screen
    .getAllByRole('button')
    .map((b) => b.textContent ?? '')
    .join('\n')
}

describe('HostList ordering', () => {
  it('keeps the default active-first alphabetical order without attention', () => {
    renderList([
      host({ hostname: 'vm-b' }),
      host({ hostname: 'vm-z', is_active: false }),
      host({ hostname: 'vm-a' }),
    ])

    const text = buttonOrder()
    expect(text.indexOf('vm-a')).toBeLessThan(text.indexOf('vm-b'))
    expect(text.indexOf('vm-b')).toBeLessThan(text.indexOf('vm-z'))
  })

  it('orders by fleet priority when attention order is on', () => {
    const hosts = [
      host({ hostname: 'vm-stale', last_seen_at: old() }),
      host({ hostname: 'vm-ok' }),
      host({ hostname: 'vm-upd', status: 'updates_available' }),
      host({ hostname: 'vm-unhealthy', health_status: 'unhealthy' }),
      host({ hostname: 'vm-sec', status: 'security_updates_available' }),
      host({ hostname: 'vm-unknown', health_status: 'unknown' }),
      host({ hostname: 'vm-degraded', health_status: 'degraded' }),
      host({ hostname: 'vm-reboot', reboot_required: true }),
    ]
    renderList(hosts, true)

    const text = buttonOrder()
    const order = [
      'vm-unhealthy',
      'vm-sec',
      'vm-degraded',
      'vm-reboot',
      'vm-upd',
      'vm-unknown',
      'vm-stale',
      'vm-ok',
    ]
    for (let i = 0; i < order.length - 1; i++) {
      expect(text.indexOf(order[i])).toBeLessThan(text.indexOf(order[i + 1]))
    }
  })
})

describe('HostList selection', () => {
  function renderSelected() {
    return render(
      <HostList
        hosts={[host({ hostname: 'vm-a' }), host({ hostname: 'vm-b' })]}
        selectedId="vm-a"
        onSelect={() => {}}
        checkedIds={new Set()}
        onToggleCheck={() => {}}
      />,
    )
  }

  it('marks only the selected row with a position-stable witness', () => {
    const { container } = renderSelected()
    const witnesses = container.querySelectorAll('.bg-zinc-200.w-0\\.5')
    expect(witnesses).toHaveLength(1)
    expect(screen.getByText('vm-a').className).toContain('text-zinc-100')
  })

  it('shows no witness without a selection', () => {
    const { container } = render(
      <HostList
        hosts={[host({ hostname: 'vm-a' })]}
        selectedId={null}
        onSelect={() => {}}
        checkedIds={new Set()}
        onToggleCheck={() => {}}
      />,
    )
    expect(container.querySelector('.bg-zinc-200.w-0\\.5')).toBeNull()
  })
})
