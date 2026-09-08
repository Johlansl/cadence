import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock } from '../test/harness'
import type { PackageSummaryRow } from '../types'
import { PackagesView } from './PackagesView'

const PACKAGES_URL = 'GET /api/v1/packages'

function row(over: Partial<PackageSummaryRow> = {}): PackageSummaryRow {
  return {
    name: 'openssl',
    architecture: 'amd64',
    hosts: [
      {
        host_id: 'h1',
        hostname: 'vm-a',
        installed_version: '1.1.1',
        candidate_version: '1.1.2',
        is_security_update: true,
        update_origin: 'Debian-Security:13/stable-security',
        updated_at: '2026-01-01T00:00:00Z',
        advisories: [],
      },
    ],
    ...over,
  }
}

const lastUrl = (fetchMock: ReturnType<typeof installFetchMock>) =>
  String(fetchMock.mock.calls[fetchMock.mock.calls.length - 1]?.[0])

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('PackagesView', () => {
  it('fetches pending packages by default and lists affected hosts', async () => {
    const fetchMock = installFetchMock({ [PACKAGES_URL]: { body: [row()] } })
    render(<PackagesView onSelectHost={() => {}} />)

    expect(await screen.findByText('openssl')).toBeInTheDocument()
    expect(screen.getByText('vm-a')).toBeInTheDocument()
    expect(screen.getByText('SEC')).toBeInTheDocument()
    expect(lastUrl(fetchMock)).toContain('status=pending')
  })

  it('shows "Load more" only when a full page came back, and appends the next page', async () => {
    const page1 = Array.from({ length: 50 }, (_, i) =>
      row({ name: `p${String(i).padStart(3, '0')}` }),
    )
    const page2 = [row({ name: 'zzz-a' }), row({ name: 'zzz-b' })]
    const fetchMock = installFetchMock({
      [PACKAGES_URL]: (req) => (req.url.includes('after=') ? { body: page2 } : { body: page1 }),
    })
    render(<PackagesView onSelectHost={() => {}} />)

    expect(await screen.findByText('p000')).toBeInTheDocument()
    const more = await screen.findByRole('button', { name: 'Load more' })

    await userEvent.click(more)

    expect(await screen.findByText('zzz-a')).toBeInTheDocument()
    expect(screen.getByText('p049')).toBeInTheDocument() // page 1 still shown
    expect(lastUrl(fetchMock)).toContain('after=p049')
    expect(lastUrl(fetchMock)).toContain('after_id=amd64')
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument(),
    )
  })

  it('shows no "Load more" when the first page is not full', async () => {
    installFetchMock({ [PACKAGES_URL]: { body: [row()] } })
    render(<PackagesView onSelectHost={() => {}} />)
    expect(await screen.findByText('openssl')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  })

  it('shows a friendly empty state', async () => {
    installFetchMock({ [PACKAGES_URL]: { body: [] } })
    render(<PackagesView onSelectHost={() => {}} />)
    expect(await screen.findByText('No packages with a pending update.')).toBeInTheDocument()
  })

  it('changing the status filter refetches with the new status', async () => {
    const fetchMock = installFetchMock({ [PACKAGES_URL]: { body: [] } })
    render(<PackagesView onSelectHost={() => {}} />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    await userEvent.selectOptions(screen.getByLabelText('Filter by status'), 'all')

    await waitFor(() => expect(lastUrl(fetchMock)).toContain('status=all'))
  })

  it('debounces the name filter before refetching', async () => {
    const fetchMock = installFetchMock({ [PACKAGES_URL]: { body: [] } })
    render(<PackagesView onSelectHost={() => {}} />)
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))

    await userEvent.type(screen.getByLabelText('Filter packages by name'), 'ssl')

    await waitFor(() => expect(lastUrl(fetchMock)).toContain('name=ssl'))
  })

  it('clicking a hostname selects that host', async () => {
    installFetchMock({ [PACKAGES_URL]: { body: [row()] } })
    const onSelectHost = vi.fn()
    render(<PackagesView onSelectHost={onSelectHost} />)

    await userEvent.click(await screen.findByText('vm-a'))
    expect(onSelectHost).toHaveBeenCalledWith('h1')
  })

  it('links a linked advisory next to the SEC pill', async () => {
    const withAdvisory = row({
      hosts: [
        {
          host_id: 'h1',
          hostname: 'vm-a',
          installed_version: '3.0.11',
          candidate_version: '3.0.14-1~deb12u2',
          is_security_update: true,
          update_origin: 'Debian-Security:12/stable-security',
          updated_at: '2026-01-01T00:00:00Z',
          advisories: [
            {
              id: 'DSA-5745-1',
              url: 'https://security-tracker.debian.org/tracker/DSA-5745-1',
              cves: ['CVE-2026-6119'],
            },
          ],
        },
      ],
    })
    installFetchMock({ [PACKAGES_URL]: { body: [withAdvisory] } })
    render(<PackagesView onSelectHost={() => {}} />)

    const link = await screen.findByRole('link', { name: 'DSA-5745-1' })
    expect(link).toHaveAttribute('href', 'https://security-tracker.debian.org/tracker/DSA-5745-1')
    expect(link).toHaveAttribute('title', 'CVE-2026-6119')
  })
})
