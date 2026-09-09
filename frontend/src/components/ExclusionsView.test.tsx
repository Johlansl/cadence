import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { HostSummary, PackageExclusion } from '../types'
import { ExclusionsView } from './ExclusionsView'

function exclusion(over: Partial<PackageExclusion> = {}): PackageExclusion {
  return {
    id: 'ex1',
    scope: 'global',
    host_id: null,
    pattern: 'linux-image*',
    description: null,
    created_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

function host(over: Partial<HostSummary> = {}): HostSummary {
  return {
    id: 'h1',
    hostname: 'vm-japp',
    fqdn: null,
    description: null,
    os_family: 'debian',
    os_name: 'Debian',
    os_version: '13',
    package_manager: 'apt',
    agent_version: '0.10.0',
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

const LIST = 'GET /api/v1/package-exclusions'
const HOSTS = 'GET /api/v1/hosts'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('ExclusionsView', () => {
  it('lists rules, global and host-scoped', async () => {
    installFetchMock({
      [LIST]: {
        body: [
          exclusion({ description: 'never touch kernels' }),
          exclusion({ id: 'ex2', scope: 'host', host_id: 'h1', pattern: 'postgresql-14' }),
        ],
      },
      [HOSTS]: { body: [host()] },
    })
    renderWithProviders(<ExclusionsView />)

    expect(await screen.findByText('linux-image*')).toBeInTheDocument()
    expect(screen.getByText('never touch kernels')).toBeInTheDocument()
    expect(screen.getByText('postgresql-14')).toBeInTheDocument()
    expect(screen.getByText('vm-japp')).toBeInTheDocument() // host-scoped row shows the hostname
  })

  it('shows the empty state', async () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [] } })
    renderWithProviders(<ExclusionsView />)
    expect(await screen.findByText('No exclusion rules configured.')).toBeInTheDocument()
  })

  it('creates a global rule', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/package-exclusions': { status: 201, body: exclusion() },
    })
    renderWithProviders(<ExclusionsView />)
    await screen.findByText('No exclusion rules configured.')

    const createBtn = screen.getByRole('button', { name: 'create' })
    expect(createBtn).toBeDisabled()

    await userEvent.type(screen.getByLabelText('Pattern'), 'linux-image*')
    expect(createBtn).toBeEnabled()
    await userEvent.click(createBtn)

    expect(await screen.findByText('Exclusion rule created.')).toBeInTheDocument()
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === 'POST')!
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('sekret')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      scope: 'global',
      host_id: null,
      pattern: 'linux-image*',
    })
  })

  it('requires a host before the create button enables, for a host-scoped rule', async () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [host()] } })
    renderWithProviders(<ExclusionsView />)
    await screen.findByText('No exclusion rules configured.')

    await userEvent.type(screen.getByLabelText('Pattern'), 'postgresql-14')
    await userEvent.selectOptions(screen.getByLabelText('Scope'), 'host')
    expect(screen.getByRole('button', { name: 'create' })).toBeDisabled()

    await userEvent.selectOptions(await screen.findByLabelText('Host'), 'h1')
    expect(screen.getByRole('button', { name: 'create' })).toBeEnabled()
  })

  it('deletes a rule after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [exclusion()] },
      [HOSTS]: { body: [] },
      'DELETE /api/v1/admin/package-exclusions/ex1': { status: 204 },
    })
    renderWithProviders(<ExclusionsView />)
    await screen.findByText('linux-image*')

    await userEvent.click(screen.getByRole('button', { name: 'delete' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText('Exclusion rule deleted.')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true)
  })
})
