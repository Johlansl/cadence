import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { Webhook } from '../types'
import { WebhooksView } from './WebhooksView'

function webhook(over: Partial<Webhook> = {}): Webhook {
  return {
    id: 'wh1',
    url_preview: 'https://discord.com/api/webhooks/42/•••',
    enabled: true,
    event_types: ['job.succeeded', 'job.failed'],
    description: null,
    created_at: '2026-09-01T00:00:00Z',
    updated_at: '2026-09-01T00:00:00Z',
    last_success_at: '2026-09-08T12:00:00Z',
    last_error: null,
    pending_count: 0,
    failed_count: 0,
    ...over,
  }
}

const LIST = 'GET /api/v1/webhooks'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('WebhooksView', () => {
  it('lists webhooks with the masked url and subscribed events', async () => {
    installFetchMock({ [LIST]: { body: [webhook({ description: 'ops' })] } })
    renderWithProviders(<WebhooksView />)

    expect(await screen.findByText('https://discord.com/api/webhooks/42/•••')).toBeInTheDocument()
    expect(screen.getByText('ops')).toBeInTheDocument()
    const list = screen.getByRole('list')
    expect(within(list).getByText('job.succeeded')).toBeInTheDocument()
    expect(within(list).getByText('enabled')).toBeInTheDocument()
    expect(screen.getByText(/last delivered/i)).toBeInTheDocument()
  })

  it('shows the empty state', async () => {
    installFetchMock({ [LIST]: { body: [] } })
    renderWithProviders(<WebhooksView />)
    expect(await screen.findByText('No webhooks configured.')).toBeInTheDocument()
  })

  it('creates a webhook and reveals the secret once', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      'POST /api/v1/admin/webhooks': {
        status: 201,
        body: {
          id: 'wh9',
          url: 'https://example.com/hook',
          secret: 'whsec-shown-once-abcdefg',
          enabled: true,
          event_types: ['job.failed'],
          description: null,
          created_at: '2026-09-09T00:00:00Z',
        },
      },
    })
    renderWithProviders(<WebhooksView />)
    await screen.findByText('No webhooks configured.')

    const createBtn = screen.getByRole('button', { name: 'create' })
    expect(createBtn).toBeDisabled()

    await userEvent.type(screen.getByLabelText('Webhook URL'), 'https://example.com/hook')
    await userEvent.click(screen.getByRole('checkbox', { name: /job\.failed/ }))
    expect(createBtn).toBeEnabled()
    await userEvent.click(createBtn)

    expect(await screen.findByText('whsec-shown-once-abcdefg')).toBeInTheDocument()
    expect(await screen.findByText('Webhook created.')).toBeInTheDocument()
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === 'POST')!
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('sekret')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      url: 'https://example.com/hook',
      event_types: ['job.failed'],
      enabled: true,
    })
  })

  it('disables a webhook', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [webhook()] },
      'PATCH /api/v1/admin/webhooks/wh1': { status: 200, body: webhook({ enabled: false }) },
    })
    renderWithProviders(<WebhooksView />)
    await screen.findByText('https://discord.com/api/webhooks/42/•••')

    await userEvent.click(screen.getByRole('button', { name: 'disable' }))

    expect(await screen.findByText('Webhook disabled.')).toBeInTheDocument()
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === 'PATCH')!
    expect(JSON.parse(String(init?.body))).toEqual({ enabled: false })
  })

  it('sends a test delivery', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [webhook()] },
      'POST /api/v1/admin/webhooks/wh1/test': { status: 202, body: { delivery_id: 'd1' } },
    })
    renderWithProviders(<WebhooksView />)
    await screen.findByText('https://discord.com/api/webhooks/42/•••')

    await userEvent.click(screen.getByRole('button', { name: 'send test' }))

    expect(await screen.findByText('Test delivery queued.')).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(([u, i]) => String(u).endsWith('/test') && i?.method === 'POST'),
    ).toBe(true)
  })

  it('deletes a webhook after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [webhook()] },
      'DELETE /api/v1/admin/webhooks/wh1': { status: 204 },
    })
    renderWithProviders(<WebhooksView />)
    await screen.findByText('https://discord.com/api/webhooks/42/•••')

    await userEvent.click(screen.getByRole('button', { name: 'delete' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    expect(await screen.findByText('Webhook deleted.')).toBeInTheDocument()
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, i]) => i?.method === 'DELETE')).toBe(true),
    )
  })

  it('prompts for the admin key when none is stored', async () => {
    const fetchMock = installFetchMock({
      [LIST]: { body: [webhook()] },
    })
    renderWithProviders(<WebhooksView />)
    await screen.findByText('https://discord.com/api/webhooks/42/•••')

    await userEvent.click(screen.getByRole('button', { name: 'send test' }))

    expect(await screen.findByPlaceholderText('X-Admin-Key')).toBeInTheDocument()
    expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith('/test'))).toBe(false)
  })
})
