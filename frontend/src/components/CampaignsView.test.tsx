import { act, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { Campaign, CampaignDetail, HostSummary } from '../types'
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
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)

    expect(await screen.findByText('march rollout')).toBeInTheDocument()
    expect(screen.getByText('running')).toBeInTheDocument()
    expect(screen.getByText(/stage 2\/2 · 1\/3 done · 1 skipped/)).toBeInTheDocument()
  })

  it('shows the empty state', async () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [] } })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    expect(await screen.findByText('No campaigns yet.')).toBeInTheDocument()
  })

  it('shows loading on the first fetch, not the empty state', () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [] } })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    expect(screen.getByText('loading…')).toBeInTheDocument()
    expect(screen.queryByText('No campaigns yet.')).not.toBeInTheDocument()
  })

  it('shows an error distinct from loading and empty when the first fetch fails', async () => {
    installFetchMock({ [LIST]: { status: 500, body: {} }, [HOSTS]: { body: [] } })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    expect(await screen.findByText("couldn't load campaigns.")).toBeInTheDocument()
    expect(screen.queryByText('No campaigns yet.')).not.toBeInTheDocument()
    expect(screen.queryByText('loading…')).not.toBeInTheDocument()
  })

  it('creates a draft with a tag target and parsed stages', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/campaigns': { status: 201, body: detail() },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
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
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
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
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
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

  it('opens the cancel confirmation in danger mode while pause stays neutral', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    installFetchMock({
      [LIST]: { body: [campaign({ status: 'running' })] },
      [HOSTS]: { body: [] },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('march rollout')

    // Irreversible cancel carries the danger signal; reversible pause does not.
    expect(screen.getByRole('button', { name: 'cancel' }).className).toContain('red')
    expect(screen.getByRole('button', { name: 'pause' }).className).not.toContain('red')

    await userEvent.click(screen.getByRole('button', { name: 'cancel' }))
    const dialog = await screen.findByRole('dialog')
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))
  })

  it('opens the host on hostname click and keeps the job id plain text', async () => {
    const onSelectHost = vi.fn()
    installFetchMock({
      [LIST]: { body: [campaign()] },
      [HOSTS]: { body: [] },
      'GET /api/v1/campaigns/c1': {
        body: detail({
          hosts: [
            {
              host_id: 'h1',
              hostname: 'vm-a',
              stage_index: 0,
              state: 'running',
              skip_reason: null,
              job_id: 'job-12345678',
            },
          ],
        }),
      },
    })
    renderWithProviders(<CampaignsView onSelectHost={onSelectHost} />)
    await screen.findByText('march rollout')

    await userEvent.click(screen.getByText('march rollout'))
    await userEvent.click(await screen.findByRole('button', { name: 'vm-a' }))

    expect(onSelectHost).toHaveBeenCalledWith('h1')
    expect(screen.getByText('job-1234').closest('button')).toBeNull()
  })

  it('recaps the exact hosts and parameters before creating', async () => {
    const fakeHosts = [
      { id: 'h1', hostname: 'vm-a' },
      { id: 'h2', hostname: 'vm-b' },
    ] as unknown as HostSummary[]
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: fakeHosts } })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('No campaigns yet.')

    await userEvent.type(screen.getByLabelText('Campaign name'), 'march rollout')
    await userEvent.click(screen.getByRole('radio', { name: 'pick hosts' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'vm-a' }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'vm-b' }))

    expect(screen.getByText(/target: 2 hosts \(vm-a, vm-b\)/)).toBeInTheDocument()
    expect(screen.getByText(/2 waves/)).toBeInTheDocument()
    expect(screen.getByText(/stops at the first skipped host/)).toBeInTheDocument()
    expect(screen.getByText(/server default/)).toBeInTheDocument()
  })

  it('recaps the exact tag criterion with no invented host count', async () => {
    installFetchMock({ [LIST]: { body: [] }, [HOSTS]: { body: [] } })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('No campaigns yet.')

    await userEvent.type(screen.getByLabelText('Campaign name'), 'march rollout')
    await userEvent.type(screen.getByLabelText('Tag filter'), 'env=prod')

    expect(screen.getByText(/target: tag "env=prod"/)).toBeInTheDocument()
    expect(screen.queryByText(/\d+ hosts/)).not.toBeInTheDocument()
  })

  it('marks the active stage and shows certain progress only', async () => {
    installFetchMock({
      [LIST]: {
        body: [
          campaign({
            status: 'running',
            stages: [1, '25%', 'rest'],
            current_stage_index: 1,
            hosts_total: 5,
            hosts_done: 2,
            hosts_skipped: 1,
          }),
        ],
      },
      [HOSTS]: { body: [] },
      'GET /api/v1/campaigns/c1': {
        body: detail({
          status: 'running',
          current_stage_index: 1,
          stages_detail: [
            {
              index: 0,
              size_spec: 1,
              hosts_total: 1,
              pending: 0,
              running: 0,
              done: 1,
              skipped: 0,
              orphaned: 0,
            },
            {
              index: 1,
              size_spec: '25%',
              hosts_total: 2,
              pending: 1,
              running: 0,
              done: 1,
              skipped: 1,
              orphaned: 0,
            },
            {
              index: 2,
              size_spec: 'rest',
              hosts_total: 2,
              pending: 2,
              running: 0,
              done: 0,
              skipped: 0,
              orphaned: 0,
            },
          ],
        }),
      },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('march rollout')

    expect(screen.getByText(/stage 2\/3 · 2\/5 done · 1 skipped/)).toBeInTheDocument()
    const params = screen.getByText('march rollout').closest('li')?.textContent ?? ''
    for (const part of ['concurrency', '2', 'max failures', '1', 'window', '600s']) {
      expect(params).toContain(part)
    }

    await userEvent.click(screen.getByText('march rollout'))
    const active = await screen.findByText('active')
    expect(active.closest('div')?.textContent).toContain('stage 2')
    // Skipped failures show both in the header progress and in the stage box.
    expect(screen.getAllByText(/1 skipped/).length).toBeGreaterThanOrEqual(2)
  })

  it('shows halt_reason without suggesting unavailable actions', async () => {
    installFetchMock({
      [LIST]: {
        body: [campaign({ status: 'stopped', halt_reason: 'dpkg_error on vm-a' })],
      },
      [HOSTS]: { body: [] },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('march rollout')

    expect(screen.getByText(/stopped: dpkg_error on vm-a/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'pause' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'resume' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'cancel' })).not.toBeInTheDocument()
  })

  it('resumes a paused campaign directly with a neutral action', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    installFetchMock({
      [LIST]: { body: [campaign({ status: 'paused' })] },
      [HOSTS]: { body: [] },
      'POST /api/v1/admin/campaigns/c1/resume': {
        status: 200,
        body: detail({ status: 'running' }),
      },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('march rollout')

    const resume = screen.getByRole('button', { name: 'resume' })
    expect(resume.className).not.toContain('red')
    await userEvent.click(resume)

    expect(await screen.findByText('Campaign resumed.')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('keeps the listed campaigns and flags sync error when a refresh fails', async () => {
    vi.useFakeTimers()
    try {
      let fail = false
      installFetchMock({
        [LIST]: () =>
          fail ? { status: 500, body: {} } : { body: [campaign({ status: 'running' })] },
        [HOSTS]: { body: [] },
      })
      renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
      await act(async () => {
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      expect(screen.getByText('march rollout')).toBeInTheDocument()

      fail = true
      await act(async () => {
        vi.advanceTimersByTime(30_000)
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      // The existing campaign stays listed with a sync error: never replaced
      // by the empty, loading, or initial-error states.
      expect(screen.getByText('march rollout')).toBeInTheDocument()
      expect(screen.getByText(/sync error/)).toBeInTheDocument()
      expect(screen.queryByText('No campaigns yet.')).not.toBeInTheDocument()
      expect(screen.queryByText('loading…')).not.toBeInTheDocument()
      expect(screen.queryByText("couldn't load campaigns.")).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('expands a row to show the per-host table', async () => {
    installFetchMock({
      [LIST]: { body: [campaign()] },
      [HOSTS]: { body: [] },
      'GET /api/v1/campaigns/c1': { body: detail() },
    })
    renderWithProviders(<CampaignsView onSelectHost={() => {}} />)
    await screen.findByText('march rollout')

    await userEvent.click(screen.getByText('march rollout'))

    expect(await screen.findByText('vm-a')).toBeInTheDocument()
    expect(screen.getByText('vm-b')).toBeInTheDocument()
  })
})
