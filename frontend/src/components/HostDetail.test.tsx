import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { HostDetail as HostDetailData } from '../types'
import { ConfirmProvider } from './ConfirmDialog'
import { HostDetail } from './HostDetail'
import { ToastProvider } from './Toast'

function host(over: Partial<HostDetailData> = {}): HostDetailData {
  return {
    id: 'h1',
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
    packages: [],
    ...over,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
  localStorage.clear()
})

function tree(h: HostDetailData, onChanged: () => void) {
  return (
    <ToastProvider>
      <ConfirmProvider>
        <HostDetail host={h} onChanged={onChanged} onDeleted={() => {}} />
      </ConfirmProvider>
    </ToastProvider>
  )
}

type MockRoutes = Parameters<typeof installFetchMock>[0]

function renderDetail(
  over: Partial<HostDetailData> = {},
  onChanged: () => void = () => {},
  extraRoutes: MockRoutes = {},
) {
  const fetchMock = installFetchMock({
    'GET /api/v1/hosts/h1/jobs': { body: [] },
    'GET /api/v1/hosts/h1/reports': { body: [] },
    'GET /api/v1/hosts/h1/schedules': { body: [] },
    ...extraRoutes,
  })
  const view = renderWithProviders(tree(host(over), onChanged))
  return { fetchMock, ...view }
}

const patchCalls = (fetchMock: ReturnType<typeof installFetchMock>) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')

function policyBlock() {
  return screen.getByText('Reboot policy').closest('div')!
}

function policySelect() {
  return within(policyBlock()).getByRole('combobox')
}

function policySave() {
  return within(policyBlock()).queryByRole('button', { name: 'save' })
}

describe('HostDetail destructive confirmation', () => {
  it('marks delete as danger and opens its confirmation with focus on Cancel', async () => {
    renderDetail()

    // Irreversible delete carries the danger signal; reversible retire does not.
    expect(screen.getByRole('button', { name: 'delete' }).className).toContain('red')
    expect(screen.getByRole('button', { name: 'retire' }).className).not.toContain('red')

    await userEvent.click(screen.getByRole('button', { name: 'delete' }))
    const dialog = await screen.findByRole('dialog')
    expect(screen.getByText('Delete this host?')).toBeInTheDocument()
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))
  })

  it('shows the server policy with no save when clean', () => {
    renderDetail()

    expect(policySelect()).toHaveDisplayValue('never')
    expect(policySave()).not.toBeInTheDocument()
  })

  it('stages a local change with no API call until Save', async () => {
    const { fetchMock } = renderDetail()

    await userEvent.selectOptions(policySelect(), 'auto')

    expect(policySelect()).toHaveDisplayValue('auto')
    expect(patchCalls(fetchMock)).toHaveLength(0)
    expect(within(policyBlock()).getByRole('button', { name: 'save' })).toBeInTheDocument()
  })

  it('clears dirty when reverting to the server value with no call', async () => {
    const { fetchMock } = renderDetail()

    await userEvent.selectOptions(policySelect(), 'auto')
    expect(policySave()).toBeInTheDocument()
    await userEvent.selectOptions(policySelect(), 'never')

    expect(policySave()).not.toBeInTheDocument()
    expect(patchCalls(fetchMock)).toHaveLength(0)
  })

  it('sends one PATCH with the new value and cleans up after refresh', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const onChanged = vi.fn()
    const { fetchMock, rerender } = renderDetail({}, onChanged, {
      'PATCH /api/v1/admin/hosts/h1': { status: 200, body: {} },
    })

    await userEvent.selectOptions(policySelect(), 'auto')
    await userEvent.click(within(policyBlock()).getByRole('button', { name: 'save' }))

    await waitFor(() => expect(patchCalls(fetchMock)).toHaveLength(1))
    const [, init] = patchCalls(fetchMock)[0]
    expect(JSON.parse(String(init?.body))).toEqual({ reboot_policy: 'auto' })
    expect(onChanged).toHaveBeenCalled()

    rerender(tree(host({ reboot_policy: 'auto' }), onChanged))
    expect(policySelect()).toHaveDisplayValue('auto')
    expect(policySave()).not.toBeInTheDocument()
  })

  it('resyncs to a newer server value instead of keeping a stale draft', async () => {
    const { fetchMock, rerender } = renderDetail()

    await userEvent.selectOptions(policySelect(), 'auto')
    expect(policySave()).toBeInTheDocument()

    rerender(tree(host({ reboot_policy: 'prompt' }), () => {}))

    expect(policySelect()).toHaveDisplayValue('prompt')
    expect(policySave()).not.toBeInTheDocument()
    expect(patchCalls(fetchMock)).toHaveLength(0)
  })

  it('keeps the draft and shows the existing error when Save fails', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const onChanged = vi.fn()
    renderDetail({}, onChanged, {
      'PATCH /api/v1/admin/hosts/h1': { status: 500, body: {} },
    })

    await userEvent.selectOptions(policySelect(), 'auto')
    await userEvent.click(within(policyBlock()).getByRole('button', { name: 'save' }))

    expect(await screen.findByText(/request failed/i)).toBeInTheDocument()
    expect(policySelect()).toHaveDisplayValue('auto')
    expect(policySave()).toBeInTheDocument()
    expect(onChanged).not.toHaveBeenCalled()
  })

  it('renders Jobs before History', () => {
    renderDetail()

    const jobs = screen.getByRole('button', { name: /jobs/i })
    const history = screen.getByRole('heading', { name: /history/i })
    expect(jobs.compareDocumentPosition(history) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('opens the reversible retire confirmation in neutral mode', async () => {
    renderDetail()

    await userEvent.click(screen.getByRole('button', { name: 'retire' }))
    const dialog = await screen.findByRole('dialog')
    expect(screen.getByText('Retire this host?')).toBeInTheDocument()
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Retire' }))
  })
})

describe('HostDetail visual hierarchy', () => {
  it('contrasts operational metadata above administrative context', () => {
    renderDetail()

    expect(screen.getByText('Last report').nextElementSibling?.className).toContain('text-zinc-100')
    expect(screen.getByText('Updates').nextElementSibling?.className).toContain('text-zinc-100')
    expect(screen.getByText('OS').nextElementSibling?.className).toContain('text-zinc-400')
    expect(screen.getByText('FQDN').nextElementSibling?.className).toContain('text-zinc-400')
  })

  it('separates destructive actions from identity and keeps reboot operational', () => {
    renderDetail({ reboot_required: true })

    const retire = screen.getByRole('button', { name: 'retire' })
    expect(retire.closest('div.border-l')).not.toBeNull()
    expect(screen.getByRole('button', { name: 'delete' }).className).toContain('red')
    expect(screen.getByRole('button', { name: 'reboot now' }).className).toContain('orange')
  })
})
