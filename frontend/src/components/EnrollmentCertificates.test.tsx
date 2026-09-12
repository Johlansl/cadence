import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import type { AgentCertificate, HostSummary } from '../types'
import { EnrollmentCertificates } from './EnrollmentCertificates'

const hosts = [
  { id: 'h1', hostname: 'vm-japp', is_active: true } as HostSummary,
  { id: 'h2', hostname: 'vm-retired', is_active: false } as HostSummary,
]

function certificate(over: Partial<AgentCertificate> = {}): AgentCertificate {
  return {
    id: 7,
    host_id: 'h1',
    serial_number: '123456789',
    fingerprint_sha256: 'aabbccddeeff',
    not_before: '2026-09-12T10:00:00Z',
    expires_at: '2027-09-12T10:00:00Z',
    issued_at: '2026-09-12T10:00:00Z',
    last_used_at: '2026-09-12T10:15:00Z',
    revoked_at: null,
    state: 'active',
    ...over,
  }
}

afterEach(() => sessionStorage.clear())

describe('EnrollmentCertificates', () => {
  it('waits for a host selection before loading protected data', () => {
    const fetchMock = installFetchMock({})
    renderWithProviders(<EnrollmentCertificates hosts={hosts} />)

    expect(screen.getByText('Select a host to inspect its certificates.')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('lists certificates and exposes inactive hosts for audit', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const fetchMock = installFetchMock({
      'GET /api/v1/admin/hosts/h1/certificates': { body: [certificate()] },
    })
    renderWithProviders(<EnrollmentCertificates hosts={hosts} />)

    expect(screen.getByRole('option', { name: 'vm-retired (inactive)' })).toBeInTheDocument()
    await userEvent.selectOptions(screen.getByLabelText('Certificate host'), 'h1')

    expect(await screen.findByText('aabbccddeeff')).toBeInTheDocument()
    expect(screen.getByText('serial 123456789')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v1/admin/hosts/h1/certificates',
      expect.objectContaining({ headers: expect.objectContaining({ 'X-Admin-Key': 'adm' }) }),
    )
  })

  it('prompts for the admin key after selecting a host', async () => {
    const fetchMock = installFetchMock({})
    renderWithProviders(<EnrollmentCertificates hosts={hosts} />)

    await userEvent.selectOptions(screen.getByLabelText('Certificate host'), 'h1')

    expect(await screen.findByPlaceholderText('X-Admin-Key')).toBeInTheDocument()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('revokes an active certificate only after confirmation', async () => {
    sessionStorage.setItem('cadence.adminKey', 'adm')
    const fetchMock = installFetchMock({
      'GET /api/v1/admin/hosts/h1/certificates': { body: [certificate()] },
      'DELETE /api/v1/admin/hosts/h1/certificates/7': { status: 204 },
    })
    renderWithProviders(<EnrollmentCertificates hosts={hosts} />)
    await userEvent.selectOptions(screen.getByLabelText('Certificate host'), 'h1')
    await screen.findByText('aabbccddeeff')

    await userEvent.click(screen.getByRole('button', { name: 'revoke certificate' }))
    const dialog = await screen.findByRole('dialog')
    expect(
      within(dialog).getByText(/immediately lose access to the mTLS agent port/),
    ).toBeInTheDocument()
    await userEvent.click(within(dialog).getByRole('button', { name: 'Revoke certificate' }))

    expect(await screen.findByText('Client certificate revoked.')).toBeInTheDocument()
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(true),
    )
  })
})
