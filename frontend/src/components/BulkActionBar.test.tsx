import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { installFetchMock, renderWithProviders } from '../test/harness'
import { BulkActionBar } from './BulkActionBar'

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

function renderBar(hostIds: string[] = ['a', 'b', 'c'], hiddenCount = 0) {
  const onClear = vi.fn()
  const onDone = vi.fn()
  renderWithProviders(
    <BulkActionBar hostIds={hostIds} hiddenCount={hiddenCount} onClear={onClear} onDone={onDone} />,
  )
  return { onClear, onDone }
}

describe('BulkActionBar upgrade confirmation', () => {
  it('asks with the target count and sends nothing on Cancel', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({ 'POST *': { status: 201, body: {} } })
    renderBar()

    await userEvent.click(screen.getByRole('button', { name: /trigger dist-upgrade/i }))
    const dialog = await screen.findByRole('dialog')
    expect(screen.getByText('Upgrade 3 hosts?')).toBeInTheDocument()
    // Danger action: focus rests on Cancel, like bulk reboot.
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))

    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(fetchMock.mock.calls.filter(([, i]) => i?.method === 'POST')).toHaveLength(0)
  })

  it('fans out to every selected host on Confirm as before', async () => {
    sessionStorage.setItem('cadence.adminKey', 'sekret')
    const fetchMock = installFetchMock({ 'POST *': { status: 201, body: {} } })
    const { onDone } = renderBar()

    await userEvent.click(screen.getByRole('button', { name: /trigger dist-upgrade/i }))
    const dialog = await screen.findByRole('dialog')
    await userEvent.click(within(dialog).getByRole('button', { name: 'Upgrade all' }))

    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([, i]) => i?.method === 'POST')).toHaveLength(3),
    )
    expect(await screen.findByText('Bulk upgrade: 3 queued.')).toBeInTheDocument()
    expect(onDone).toHaveBeenCalled()
  })
})

describe('BulkActionBar hidden selection', () => {
  it('reports total plus hidden count when filters hide selected hosts', () => {
    renderBar(['a', 'b', 'c'], 2)

    expect(screen.getByText(/3 selected/)).toBeInTheDocument()
    expect(screen.getByText(/2 hidden by filters/)).toBeInTheDocument()
  })

  it('shows only the total when nothing is hidden', () => {
    renderBar(['a', 'b'], 0)

    expect(screen.getByText(/2 selected/)).toBeInTheDocument()
    expect(screen.queryByText(/hidden/)).not.toBeInTheDocument()
  })
})
