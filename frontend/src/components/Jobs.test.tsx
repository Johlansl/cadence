import { act, screen, waitFor, within } from '@testing-library/react'
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

describe('Jobs loading / empty / stale', () => {
  it('shows loading on the first fetch, not the empty state', () => {
    installFetchMock({ [JOBS_URL]: { body: [] } })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(screen.getByText('loading…')).toBeInTheDocument()
    expect(screen.queryByText('No jobs yet.')).not.toBeInTheDocument()
  })

  it('shows the empty state only after a successful fetch returns no jobs', async () => {
    installFetchMock({ [JOBS_URL]: { body: [] } })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('No jobs yet.')).toBeInTheDocument()
    expect(screen.queryByText('loading…')).not.toBeInTheDocument()
  })

  it('shows an error distinct from the empty state when the first fetch fails', async () => {
    installFetchMock({ [JOBS_URL]: { status: 500, body: {} } })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText("couldn't load jobs.")).toBeInTheDocument()
    expect(screen.queryByText('No jobs yet.')).not.toBeInTheDocument()
    expect(screen.queryByText('loading…')).not.toBeInTheDocument()
  })

  it('keeps the loaded jobs and marks them stale when a refresh fails', async () => {
    vi.useFakeTimers()
    try {
      let fail = false
      installFetchMock({
        [JOBS_URL]: () => (fail ? { status: 500, body: {} } : { body: [job()] }),
      })
      renderWithProviders(<Jobs hostId="h1" />)
      await act(async () => {
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      expect(screen.getByText('apt_upgrade')).toBeInTheDocument()

      fail = true
      await act(async () => {
        vi.advanceTimersByTime(15_000)
        for (let i = 0; i < 20; i++) await Promise.resolve()
      })
      // Previous data stays on screen, flagged as stale instead of vanishing.
      expect(screen.getByText('apt_upgrade')).toBeInTheDocument()
      expect(screen.getByText(/stale/)).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('Jobs destructive confirmation', () => {
  it('opens the clear confirmation in danger mode with focus on Cancel', async () => {
    installFetchMock({ [JOBS_URL]: { body: [job()] } })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('apt_upgrade')

    await userEvent.click(screen.getByRole('button', { name: 'clear' }))
    const dialog = await screen.findByRole('dialog')
    expect(screen.getByText('Clear job history?')).toBeInTheDocument()
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))
  })
})

describe('Jobs verdict and details', () => {
  it('shows the failure verdict with visible details for a failed job', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            status: 'failed',
            failure_category: 'dpkg_error',
            failure_summary: 'boom summary',
            requested_by: 'dashboard',
            result: { exit_code: 1, held_conflicts: ['docker-ce'] },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('apt_upgrade')

    expect(screen.getByText('dpkg error')).toBeInTheDocument()
    expect(screen.getByText('boom summary')).toBeInTheDocument()
    expect(screen.getByText(/hold conflict: docker-ce/)).toBeInTheDocument()
    expect(screen.getByText(/exit 1/)).toBeInTheDocument()
    // Verdict line precedes the technical details in the DOM.
    const html = screen.getByText('apt_upgrade').closest('li')!.innerHTML
    expect(html.indexOf('dpkg error')).toBeLessThan(html.indexOf('by dashboard'))
  })

  it('explains disabled actions while a job is active', async () => {
    installFetchMock({ [JOBS_URL]: { body: [job({ status: 'running' })] } })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('apt_upgrade')

    expect(screen.getByRole('button', { name: /dry run/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /health check/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /job in progress/i })).toBeDisabled()
    expect(screen.getByText(/actions are available again when it finishes/)).toBeInTheDocument()
  })

  it('labels the raw output as technical log', async () => {
    installFetchMock({ [JOBS_URL]: { body: [job({ log: 'line1' })] } })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('apt_upgrade')

    expect(screen.getByText('technical log')).toBeInTheDocument()
  })
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

  it('keeps action success distinct from unhealthy post-checks', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            status: 'succeeded',
            result: {
              exit_code: 0,
              health_status: 'unhealthy',
              pre_checks: {
                status: 'passed',
                checks: [
                  {
                    name: 'disk_space',
                    status: 'passed',
                    summary: 'Enough disk space is available.',
                    details: {
                      filesystems: [
                        {
                          paths: ['/'],
                          available_bytes: 2147483648,
                          minimum_available_bytes: 1073741824,
                        },
                      ],
                    },
                  },
                ],
              },
              post_checks: {
                status: 'failed',
                checks: [
                  {
                    name: 'failed_services',
                    status: 'failed',
                    summary: 'A service failed after the upgrade.',
                    details: { new_services: ['nginx.service'] },
                  },
                ],
              },
            },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('succeeded')).toBeInTheDocument()
    expect(screen.getByText('health: unhealthy')).toBeInTheDocument()
    expect(screen.getByText('Before upgrade')).toBeInTheDocument()
    expect(screen.getByText('After upgrade')).toBeInTheDocument()
    expect(screen.getByText('newly failed: nginx.service')).toBeInTheDocument()
    expect(screen.getByText('/: 2.0 GiB available, 1.0 GiB required')).toBeInTheDocument()
  })

  it('explains when blocking pre-checks prevent post-checks', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            status: 'failed',
            failure_category: 'apt_locked',
            result: {
              exit_code: -1,
              health_status: 'unknown',
              pre_checks: {
                status: 'failed',
                checks: [
                  {
                    name: 'package_manager_locks',
                    status: 'failed',
                    summary: 'A package manager lock is held.',
                    details: { locks: [{ path: '/var/lib/dpkg/lock', pid: 42 }] },
                  },
                ],
              },
            },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(
      await screen.findByText('Not run because a pre-check blocked the upgrade.'),
    ).toBeInTheDocument()
    expect(screen.getByText('/var/lib/dpkg/lock (PID 42)')).toBeInTheDocument()
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

  it('queues a health_check job from the health check button', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({
      [JOBS_URL]: { body: [] },
      [POST_URL]: { status: 201, body: job({ job_type: 'health_check', status: 'pending' }) },
    })
    renderWithProviders(<Jobs hostId="h1" />)
    await screen.findByText('No jobs yet.')

    await userEvent.click(screen.getByRole('button', { name: /health check/i }))

    expect(await screen.findByText('Health check queued.')).toBeInTheDocument()
    const [, init] = postCalls(fetchMock)[0]
    expect(JSON.parse(String(init?.body)).job_type).toBe('health_check')
  })

  it('renders a health_check result as a single checks panel, not before/after', async () => {
    installFetchMock({
      [JOBS_URL]: {
        body: [
          job({
            job_type: 'health_check',
            status: 'succeeded',
            result: {
              exit_code: 0,
              health_status: 'degraded',
              post_checks: {
                status: 'warning',
                checks: [
                  {
                    name: 'reboot_required',
                    status: 'warning',
                    summary: 'a reboot is required',
                    details: {},
                  },
                ],
              },
            },
          }),
        ],
      },
    })
    renderWithProviders(<Jobs hostId="h1" />)

    expect(await screen.findByText('health check')).toBeInTheDocument()
    expect(screen.queryByText('Before upgrade')).not.toBeInTheDocument()
    expect(screen.queryByText('After upgrade')).not.toBeInTheDocument()
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
