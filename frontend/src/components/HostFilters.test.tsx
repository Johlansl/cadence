import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { EMPTY_FILTERS } from '../lib/hostFilter'
import { HostFilters } from './HostFilters'

describe('HostFilters labels and attention toggle', () => {
  it('labels the freshness filter late or silent, not overdue', () => {
    render(<HostFilters value={EMPTY_FILTERS} onChange={() => {}} shown={3} total={5} />)

    expect(screen.getByText('late or silent')).toBeInTheDocument()
    expect(screen.queryByText('overdue')).not.toBeInTheDocument()
  })

  it('emits the attention flag when the needs-attention box is toggled', async () => {
    const onChange = vi.fn()
    render(<HostFilters value={EMPTY_FILTERS} onChange={onChange} shown={3} total={5} />)

    await userEvent.click(screen.getByRole('checkbox', { name: 'needs attention' }))

    expect(onChange).toHaveBeenCalledWith({ ...EMPTY_FILTERS, attention: true })
  })
})
