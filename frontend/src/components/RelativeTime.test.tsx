import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RelativeTime } from './RelativeTime'

afterEach(() => {
  vi.useRealTimers()
})

describe('RelativeTime', () => {
  it('renders the relative label and refreshes itself on the interval', () => {
    vi.useFakeTimers()
    const thirtySecondsAgo = new Date(Date.now() - 30_000).toISOString()

    render(<RelativeTime iso={thirtySecondsAgo} />)
    expect(screen.getByText('30s ago')).toBeInTheDocument()

    act(() => {
      vi.advanceTimersByTime(15_000)
    })
    expect(screen.getByText('45s ago')).toBeInTheDocument()
  })

  it('renders "never" for a null timestamp', () => {
    render(<RelativeTime iso={null} />)
    expect(screen.getByText('never')).toBeInTheDocument()
  })
})
