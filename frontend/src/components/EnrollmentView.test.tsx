import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { EnrollmentCode, HostSummary } from '../types'
import { EnrollmentView, parseEnrollmentTags } from './EnrollmentView'

const LIST = 'GET /api/v1/admin/enrollments'

const hosts = [
  {
    id: 'h1',
    hostname: 'vm-existing',
    is_active: true,
  } as HostSummary,
  {
    id: 'h2',
    hostname: 'vm-disabled',
    is_active: false,
  } as HostSummary,
]

function enrollment(over: Partial<EnrollmentCode> = {}): EnrollmentCode {
  return {
    id: 'en1',
    target_host_id: null,
    enrolled_host_id: null,
    expected_hostname: 'vm-new',
    label: null,
    description: null,
    tags: {},
    reboot_policy: 'never',
    created_at: '2026-09-12T10:00:00Z',
    expires_at: '2026-09-12T10:30:00Z',
    consumed_at: null,
    revoked_at: null,
    state: 'pending',
    ...over,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

describe('parseEnrollmentTags', () => {
  it('parses comma-separated key=value tags', () => {
    expect(parseEnrollmentTags('role=web, env=lab')).toEqual({ role: 'web', env: 'lab' })
  })

  it('rejects a tag without an equals sign', () => {
    expect(() => parseEnrollmentTags('role')).toThrow('key=value')
  })
})

describe('EnrollmentView', () => {
  it('prompts for the admin key before loading protected data', async () => {
    const fetchMock = installFetchMock({})
    renderWithProviders(<EnrollmentView hosts={hosts} />)

    expect(await screen.findByPlaceholderText('X-Admin-Key')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('lists codes without ever expecting their plaintext secret', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    installFetchMock({
      [LIST]: {
        body: [
          enrollment({ target_host_id: 'h1', expected_hostname: null }),
          enrollment({ id: 'en2', state: 'consumed', consumed_at: '2026-09-12T10:05:00Z' }),
        ],
      },
    })
    renderWithProviders(<EnrollmentView hosts={hosts} />)

    const existingKind = await screen.findByText('existing host')
    expect(within(existingKind.closest('li')!).getByText('vm-existing')).toBeInTheDocument()
    expect(screen.getAllByText('consumed')).toHaveLength(2)
    expect(screen.queryByText(/cad1\./)).not.toBeInTheDocument()
  })

  it('creates a new-host code and shows it once with safe bootstrap instructions', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      'POST /api/v1/admin/enrollments': {
        status: 201,
        body: {
          id: 'en9',
          code: 'cad1.secret.fingerprint',
          expires_at: '2026-09-12T10:30:00Z',
          target_host_id: null,
          expected_hostname: 'vm-new',
        },
      },
    })
    renderWithProviders(<EnrollmentView hosts={hosts} />)
    await screen.findByText('No enrollment codes in this state.')

    await userEvent.type(screen.getByLabelText('Expected hostname'), 'vm-new')
    await userEvent.type(screen.getByLabelText('Host tags'), 'role=web, env=lab')
    await userEvent.selectOptions(screen.getByLabelText('Reboot policy'), 'prompt')
    await userEvent.click(screen.getByRole('button', { name: 'generate code' }))

    expect(await screen.findByText('cad1.secret.fingerprint')).toBeInTheDocument()
    expect(screen.getByText(/Never download it over HTTP/)).toBeInTheDocument()
    expect(screen.queryByText(/curl/i)).not.toBeInTheDocument()
    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(post[1]?.body as string)).toMatchObject({
      expected_hostname: 'vm-new',
      ttl_minutes: 30,
      tags: { role: 'web', env: 'lab' },
      reboot_policy: 'prompt',
    })

    await userEvent.click(screen.getByRole('button', { name: 'dismiss' }))
    expect(screen.queryByText('cad1.secret.fingerprint')).not.toBeInTheDocument()
  })

  it('creates a host-bound migration code without new-host fields', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const fetchMock = installFetchMock({
      [LIST]: { body: [] },
      'POST /api/v1/admin/enrollments': {
        status: 201,
        body: {
          id: 'en9',
          code: 'cad1.secret.fingerprint',
          expires_at: '2026-09-12T10:30:00Z',
          target_host_id: 'h1',
          expected_hostname: null,
        },
      },
    })
    renderWithProviders(<EnrollmentView hosts={hosts} />)
    await screen.findByText('No enrollment codes in this state.')

    await userEvent.click(screen.getByRole('radio', { name: 'migrate existing host' }))
    await userEvent.selectOptions(screen.getByLabelText('Existing host'), 'h1')
    expect(screen.queryByRole('option', { name: 'vm-disabled' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'generate code' }))

    const post = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')!
    expect(JSON.parse(post[1]?.body as string)).toEqual({
      ttl_minutes: 30,
      label: null,
      target_host_id: 'h1',
    })
  })

  it('copies the one-time code through the Clipboard API', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    installFetchMock({
      [LIST]: { body: [] },
      'POST /api/v1/admin/enrollments': {
        status: 201,
        body: {
          id: 'en9',
          code: 'cad1.secret.fingerprint',
          expires_at: '2026-09-12T10:30:00Z',
          target_host_id: null,
          expected_hostname: 'vm-new',
        },
      },
    })
    renderWithProviders(<EnrollmentView hosts={hosts} />)
    await screen.findByText('No enrollment codes in this state.')
    await userEvent.type(screen.getByLabelText('Expected hostname'), 'vm-new')
    await userEvent.click(screen.getByRole('button', { name: 'generate code' }))
    await userEvent.click(await screen.findByRole('button', { name: 'copy code' }))

    expect(writeText).toHaveBeenCalledWith('cad1.secret.fingerprint')
    expect(await screen.findByText('Enrollment code copied.')).toBeInTheDocument()
  })

  it('revokes a pending code after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const fetchMock = installFetchMock({
      [LIST]: { body: [enrollment()] },
      'DELETE /api/v1/admin/enrollments/en1': { status: 204 },
    })
    renderWithProviders(<EnrollmentView hosts={hosts} />)
    await screen.findByText('vm-new')

    await userEvent.click(screen.getByRole('button', { name: 'revoke' }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Revoke code' }))

    expect(await screen.findByText('Enrollment code revoked.')).toBeInTheDocument()
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true),
    )
  })
})
