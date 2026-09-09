import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { Job } from '../types'
import { Jobs } from './Jobs'

let seq = 0
function job(over: Partial<Job> = {}): Job {
  return {
    id: `job-${++seq}`,
    host_id: 'h1',
    job_type: 'apt_upgrade',
    status: 'succeeded',
    params: {},
    requested_by: 'dashboard',
    result: null,
    log: null,
    failure_category: null,
    failure_summary: null,
    created_at: '2026-08-01T00:00:00Z',
    started_at: null,
    completed_at: null,
    ...over,
  }
}

const JOBS_URL = 'GET /api/v1/hosts/h1/jobs'
const POST_URL = 'POST /api/v1/admin/hosts/h1/jobs'
const postCalls = (fetchMock: ReturnType<typeof installFetchMock>) =>
  fetchMock.mock.calls.filter(([, init]) => init?.method === 'POST')

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
  localStorage.clear()
})

describe('Jobs', () => {
  it('lists the jobs fetched for the host', async () => {
    installFetchMock({ [JOBS_URL]: { body: [job({ status: 'failed', log: 'boom' })] } })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('apt_upgrade')).toBeInTheDocument()
    expect(screen.getByText('failed')).toBeInTheDocument()
  })

  it('shows the failure category and summary on a failed job', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            status: 'failed',
            log: 'boom',
            failure_category: 'dpkg_error',
            failure_summary: 'E: Sub-process /usr/bin/dpkg returned an error code (1)',
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('dpkg error')).toBeInTheDocument()
    expect(
      screen.getByText('E: Sub-process /usr/bin/dpkg returned an error code (1)'),
    ).toBeInTheDocument()
  })

  it('shows no failure badge on a succeeded job', async () => {
    installFetchMock({ [JOBS_URL]: { body: [job({ status: 'succeeded' })] } })
    renderWithProviders(<Jobs hostId="h1" />)

    await screen.findByText('apt_upgrade')
    expect(screen.queryByText('dpkg error')).not.toBeInTheDocument()
  })

  it('shows a hold-conflict badge on a succeeded job with held_conflicts', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            status: 'succeeded',
            result: { exit_code: 0, held_conflicts: ['docker-ce'] },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('hold conflict')).toBeInTheDocument()
  })

  it('shows no hold-conflict badge when held_conflicts is empty', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [job({ status: 'succeeded', result: { exit_code: 0, held_conflicts: [] } })],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    await screen.findByText('apt_upgrade')
    expect(screen.queryByText('hold conflict')).not.toBeInTheDocument()
  })

  it('queues an apt_dry_run job from the dry run button', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [JOBS_URL]: { body: [] },
      [POST_URL]: { status: 201, body: job({ job_type: 'apt_dry_run', status: 'pending' }) },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('No jobs yet.')

    await userEvent.click(screen.getByRole('button', { name: /dry run/i }))

    expect(await screen.findByText('Dry-run queued.')).toBeInTheDocument()
    const [, init] = postCalls(fetchMock)[0]
    expect(JSON.parse(String(init?.body)).job_type).toBe('apt_dry_run')
  })

  it('renders the structured preview for an apt_dry_run job', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            job_type: 'apt_dry_run',
            status: 'succeeded',
            result: {
              exit_code: 0,
              dry_run: {
                updated: [
                  {
                    name: 'openssl',
                    architecture: 'amd64',
                    installed_version: '3.0.11-1',
                    candidate_version: '3.0.14-1',
                    is_security_update: true,
                  },
                ],
                newly_installed: [],
                removed: [{ name: 'obsolete-lib', installed_version: '4.5-6' }],
                kept_back: ['docker-ce'],
                excluded: ['linux-image-amd64'],
                held_in_place: ['docker-ce'],
              },
            },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText(/1 to upgrade/)).toBeInTheDocument()
    expect(screen.getByText(/1 to remove/)).toBeInTheDocument()
    expect(screen.getByText(/1 excluded by policy/)).toBeInTheDocument()
    expect(screen.getByText(/already on hold on the host: docker-ce/)).toBeInTheDocument()
  })

  it('triggers a dist-upgrade with the stored admin key and toasts', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [JOBS_URL]: { body: [] },
      [POST_URL]: { status: 201, body: job({ status: 'pending' }) },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('No jobs yet.')

    await userEvent.click(screen.getByRole('button', { name: /trigger dist-upgrade/i }))

    expect(await screen.findByText('Upgrade job queued.')).toBeInTheDocument()
    const [, init] = postCalls(fetchMock)[0]
    expect(new Headers(init?.headers).get('X-Admin-Key')).toBe('sekret')
  })

  it('prompts for the admin key when none is stored, then retries the action', async () => {
    const fetchMock = installFetchMock({
      [JOBS_URL]: { body: [] },
      [POST_URL]: { status: 201, body: job({ status: 'pending' }) },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('No jobs yet.')

    await userEvent.click(screen.getByRole('button', { name: /trigger dist-upgrade/i }))
    expect(postCalls(fetchMock)).toHaveLength(0) // prompt instead of a request

    await userEvent.type(screen.getByPlaceholderText('X-Admin-Key'), 'typed-key')
    await userEvent.click(screen.getByRole('button', { name: /save & retry/i }))

    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(1))
    expect(sessionStorage.getItem('cadence.adminKey')).toBe('typed-key')
    expect(new Headers(postCalls(fetchMock)[0][1]?.headers).get('X-Admin-Key')).toBe('typed-key')
  })

  it('clears a rejected admin key and shows an error on 401', async () => {
    sessionStorage.setItem('cadence.adminKey', 'stale')
    installFetchMock({
      [JOBS_URL]: { body: [job()] },
      [POST_URL]: { status: 401, body: { detail: 'bad key' } },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('apt_upgrade')

    await userEvent.click(screen.getByRole('button', { name: /trigger dist-upgrade/i }))

    expect(await screen.findByText('Invalid admin key.')).toBeInTheDocument()
    expect(sessionStorage.getItem('cadence.adminKey')).toBeNull()
    expect(screen.getByPlaceholderText('X-Admin-Key')).toBeInTheDocument()
  })
})
