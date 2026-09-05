import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { Schedule as ScheduleData } from '../types'
import { Schedule } from './Schedule'

function schedule(over: Partial<ScheduleData> = {}): ScheduleData {
  return {
    id: 'sch1',
    host_id: 'h1',
    enabled: true,
    kind: 'weekly',
    day_of_month: null,
    weekday: 6,
    hour: 4,
    minute: 0,
    timezone: 'UTC',
    params: {},
    last_run_at: null,
    next_run_at: '2026-09-01T04:00:00Z',
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...over,
  }
}

const LIST_URL = 'GET /api/v1/hosts/h1/schedules'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('Schedule', () => {
  it('creates a schedule from the default form when none exists', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST_URL]: { body: [] },
      'POST /api/v1/admin/hosts/h1/schedules': { status: 201, body: schedule() },
    })
    renderWithProviders(<Schedule hostId="h1" />)
    expect(await screen.findByText('none')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'create' }))

    expect(await screen.findByText('Schedule created.')).toBeInTheDocument()
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === 'POST')!
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('sekret')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      kind: 'weekly',
      weekday: 6,
      hour: 4,
      enabled: true,
      timezone: 'UTC',
      params: {},
    })
  })

  it('shows the timezone as a fixed UTC label, with no editable field', async () => {
    installFetchMock({ [LIST_URL]: { body: [] } })
    renderWithProviders(<Schedule hostId="h1" />)
    await screen.findByText('none')

    expect(screen.getByText('UTC')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).toBeNull()
  })

  it('populates the form from an existing schedule and deletes it after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST_URL]: {
        body: [schedule({ hour: 5, kind: 'monthly', day_of_month: 12, weekday: null })],
      },
      'DELETE /api/v1/admin/schedules/sch1': { status: 204 },
    })
    renderWithProviders(<Schedule hostId="h1" />)

    // Existing schedule -> the primary button flips to "update", next run shown.
    expect(await screen.findByRole('button', { name: 'update' })).toBeInTheDocument()
    expect(screen.getByText(/next run/i)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'delete' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText('Schedule deleted.')).toBeInTheDocument()
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true),
    )
  })

  it('prompts for the admin key when none is stored', async () => {
    const fetchMock = installFetchMock({
      [LIST_URL]: { body: [] },
      'POST /api/v1/admin/hosts/h1/schedules': { status: 201, body: schedule() },
    })
    renderWithProviders(<Schedule hostId="h1" />)
    await screen.findByText('none')

    await userEvent.click(screen.getByRole('button', { name: 'create' }))

    expect(await screen.findByPlaceholderText('X-Admin-Key')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([, i]) => i?.method === 'POST')).toBe(false)
  })
})
