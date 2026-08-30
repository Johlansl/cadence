import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { PackageTable } from './PackageTable'
import type { HostPackage } from '../types'

function pkg(over: Partial<HostPackage> = {}): HostPackage {
  return {
    name: 'pkg',
    architecture: 'amd64',
    installed_version: '1.0',
    candidate_version: null,
    is_security_update: false,
    update_origin: null,
    updated_at: '2026-01-01T00:00:00Z',
    ...over,
  }
}

const rowNames = () =>
  screen
    .getAllByRole('row')
    .slice(1) // drop the header row
    .map((r) => r.querySelector('td')?.textContent?.replace(/:.*/, '') ?? '')
    .filter(Boolean)

describe('PackageTable', () => {
  const pkgs = [
    pkg({ name: 'zlib', candidate_version: '1.3', is_security_update: true }),
    pkg({ name: 'acl', candidate_version: '2.3' }),
    pkg({ name: 'bash' }), // no pending update
  ]

  it('defaults to pending updates, security first', () => {
    render(<PackageTable packages={pkgs} />)
    expect(rowNames()).toEqual(['zlib', 'acl'])
    expect(screen.getByText('2 of 3 packages')).toBeInTheDocument()
  })

  it('"security only" narrows to SEC packages', async () => {
    render(<PackageTable packages={pkgs} />)
    await userEvent.click(screen.getByLabelText('security only'))
    expect(rowNames()).toEqual(['zlib'])
  })

  it('name search filters rows', async () => {
    render(<PackageTable packages={pkgs} />)
    await userEvent.type(screen.getByLabelText(/filter packages/i), 'acl')
    expect(rowNames()).toEqual(['acl'])
  })

  it('clicking a column header sorts, then restores priority order', async () => {
    render(<PackageTable packages={pkgs} />)
    const nameHeader = screen.getByRole('button', { name: /package/i })
    await userEvent.click(nameHeader) // asc by name
    expect(rowNames()).toEqual(['acl', 'zlib'])
    await userEvent.click(nameHeader) // desc
    expect(rowNames()).toEqual(['zlib', 'acl'])
    await userEvent.click(nameHeader) // back to smart (security first)
    expect(rowNames()).toEqual(['zlib', 'acl'])
  })

  it('shows a friendly empty state when the pending filter hides everything', () => {
    render(<PackageTable packages={[pkg({ name: 'bash' })]} />)
    expect(screen.getByText('No pending updates.')).toBeInTheDocument()
  })
})
