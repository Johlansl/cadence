import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConfirmProvider, useConfirm } from './ConfirmDialog'

afterEach(() => {
  vi.unstubAllGlobals()
})

function Trigger({
  opts,
  label = 'go',
}: {
  opts: Parameters<ReturnType<typeof useConfirm>>[0]
  label?: string
}) {
  const confirm = useConfirm()
  return (
    <button
      type="button"
      onClick={() => {
        void confirm(opts).then((ok) => {
          document.body.dataset.confirmResult = ok ? 'true' : 'false'
        })
      }}
    >
      {label}
    </button>
  )
}

function renderTrigger(opts: Parameters<ReturnType<typeof useConfirm>>[0]) {
  delete document.body.dataset.confirmResult
  render(
    <ConfirmProvider>
      <Trigger opts={opts} />
    </ConfirmProvider>,
  )
}

describe('ConfirmDialog danger mode', () => {
  const dangerOpts = {
    title: 'Delete this host?',
    body: 'This cannot be undone.',
    confirmLabel: 'Delete',
    danger: true,
  } as const

  it('rests the initial focus on Cancel', async () => {
    renderTrigger({ ...dangerOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))

    const dialog = await screen.findByRole('dialog')
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Cancel' }))
  })

  it('does not validate the destructive action on an immediate Enter', async () => {
    renderTrigger({ ...dangerOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))
    await screen.findByRole('dialog')

    await userEvent.keyboard('{Enter}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(document.body.dataset.confirmResult).toBe('false')
  })

  it('cancels on Escape', async () => {
    renderTrigger({ ...dangerOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))
    await screen.findByRole('dialog')

    await userEvent.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(document.body.dataset.confirmResult).toBe('false')
  })

  it('validates on an explicit Confirm click', async () => {
    renderTrigger({ ...dangerOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))
    const dialog = await screen.findByRole('dialog')

    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete' }))

    expect(document.body.dataset.confirmResult).toBe('true')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('ConfirmDialog neutral mode', () => {
  const neutralOpts = {
    title: 'Retire this host?',
    confirmLabel: 'Retire',
  } as const

  it('keeps the initial focus on Confirm', async () => {
    renderTrigger({ ...neutralOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))

    const dialog = await screen.findByRole('dialog')
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: 'Retire' }))
  })

  it('validates on Enter without moving focus', async () => {
    renderTrigger({ ...neutralOpts })
    await userEvent.click(screen.getByRole('button', { name: 'go' }))
    await screen.findByRole('dialog')

    await userEvent.keyboard('{Enter}')

    expect(document.body.dataset.confirmResult).toBe('true')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})
