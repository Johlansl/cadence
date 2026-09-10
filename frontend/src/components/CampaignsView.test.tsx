import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { Campaign, CampaignDetail } from '../types'
import { CampaignsView, parseStages } from './CampaignsView'

function campaign(over: Partial<Campaign> = {}): Campaign {
  return {
    id: 'c1',
    name: 'march rollout',
    job_type: 'apt_upgrade',
    stages: [1, 'rest'],
    max_concurrency: 2,
    max_failures: 1,
    observation_window_seconds: 600,
    status: 'draft',
    halt_reason: null,
    requested_by: 'admin',
    hosts_total: 3,
    hosts_done: 0,
    hosts_skipped: 0,
    hosts_orphaned: 0,
    current_stage_index: 0,
    created_at: '2026-09-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    updated_at: '2026-09-01T00:00:00Z',
    ...over,
  }
}

function detail(over: Partial<CampaignDetail> = {}): CampaignDetail {
  return {
    ...campaign(),
    stages_detail: [
      {
        index: 0,
        size_spec: 1,
        hosts_total: 1,
        pending: 1,
        running: 0,
        done: 0,
        skipped: 0,
        orphaned: 0,
      },
      {
        index: 1,
        size_spec: 'rest',
        hosts_total: 2,
        pending: 2,
        running: 0,
        done: 0,
        skipped: 0,
        orphaned: 0,
      },
    ],
    hosts: [
      {
        host_id: 'h1',
        hostname: 'vm-a',
        stage_index: 0,
        state: 'pending',
        skip_reason: null,
        job_id: null,
      },
      {
        host_id: 'h2',
        hostname: 'vm-b',
        stage_index: 1,
        state: 'pending',
        skip_reason: null,
        job_id: null,
      },
    ],
    ...over,
  }
}

const LIST = 'GET /api/v1/campaigns'
const HOSTS = 'GET /api/v1/hosts'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('parseStages', () => {
  it('turns bare integers into numbers and keeps %/rest as strings', () => {
    expect(parseStages('2, 25%, rest')).toEqual([2, '25%', 'rest'])
    expect(parseStages(' 1 ')).toEqual([1])
    expect(parseStages('')).toEqual([])
  })
})

describe('CampaignsView', () => {
  it('lists campaigns with status and progress', async () => {
    installFetchMock({
      [LIST]: {
        body: [
          campaign({ status: 'running', current_stage_index: 1, hosts_done: 1, hosts_skipped: 1 }),
        ],
      },
      [HOSTS]: { body: [] },
    })
    renderWithProviders(<CampaignsView />)

    expect(await screen.findByText('march rollout')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/stage 2\/2 · 1\/3 done · 1 skipped/)).toBeInTheDocument()
  })

  it('shows the empty state', async () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [] } })
    renderWithProviders(<CampaignsView />)
    expect(await screen.findByText('No campaigns yet.')).toBeInTheDocument()
  })

  it('creates a draft with a tag target and parsed stages', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/campaigns': { status: 201, body: detail() },
    })
    renderWithProviders(<CampaignsView />)
    await screen.findByText('No campaigns yet.')

    const createBtn = screen.getByRole('button', { name: 'create draft' })
    expect(createBtn).toBeDisabled()

    await userEvent.type(screen.getByLabelText('Campaign name'), 'march rollout')
    await userEvent.clear(screen.getByLabelText('Stages'))
    await userEvent.type(screen.getByLabelText('Stages'), '2, 25%, rest')
    await userEvent.type(screen.getByLabelText('Tag filter'), 'env=prod')
    expect(createBtn).toBeEnabled()
    await userEvent.click(createBtn)

    expect(await screen.findByText('Campaign created as a draft.')).toBeInTheDocument()
    const [, init] = fetchMock.mock.calls.find(([, i]) => i?.method === 'POST')!
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('sekret')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      name: 'march rollout',
      stages: [2, '25%', 'rest'],
      max_concurrency: 1,
      max_failures: 0,
      tag: 'env=prod',
    })
  })

  it('activates a draft campaign', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [campaign()] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/campaigns/c1/activate': {
        status: 200,
        body: detail({ status: 'running' }),
      },
    })
    renderWithProviders(<CampaignsView />)
    await screen.findByText('march rollout')

    await userEvent.click(screen.getByRole('button', { name: 'activate' }))

    expect(await screen.findByText('Campaign activated.')).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(
        ([u, i]) => String(u).endsWith('/campaigns/c1/activate') && i?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('cancels a running campaign after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [campaign({ status: 'running' })] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/campaigns/c1/cancel': {
        status: 200,
        body: detail({ status: 'cancelled' }),
      },
    })
    renderWithProviders(<CampaignsView />)
    await screen.findByText('march rollout')

    await userEvent.click(screen.getByRole('button', { name: 'cancel' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel campaign' }))

    expect(await screen.findByText('Campaign cancelled.')).toBeInTheDocument()
    expect(
      fetchMock.mock.calls.some(
        ([u, i]) => String(u).endsWith('/campaigns/c1/cancel') && i?.method === 'POST',
      ),
    ).toBe(true)
  })

  it('expands a row to show the per-host table', async () => {
    installFetchMock({
      [LIST]: { body: [campaign()] },
      [HOSTS]: { body: [] },
      'GET /api/v1/campaigns/c1': { body: detail() },
    })
    renderWithProviders(<CampaignsView />)
    await screen.findByText('march rollout')

    await userEvent.click(screen.getByText('march rollout'))

    expect(await screen.findByText('vm-a')).toBeInTheDocument()
    expect(screen.getByText('vm-b')).toBeInTheDocument()
  })
})
