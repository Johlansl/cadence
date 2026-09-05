import { act, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useNow } from './useNow'

afterEach(() => {
  vi.useRealTimers()
})

describe('useNow', () => {
  it('re-renders the caller on each interval and stops after unmount', () => {
    vi.useFakeTimers()
    let renders = 0
    function Probe() {
      useNow(1_000)
      renders++
      return null
    }

    const { unmount } = render(createElement(Probe))
    expect(renders).toBe(1)

    // Separate flushes: React batches every update inside one act().
    act(() => vi.advanceTimersByTime(1_000))
    act(() => vi.advanceTimersByTime(1_000))
    expect(renders).toBe(3)

    unmount()
    act(() => vi.advanceTimersByTime(5_000))
    expect(renders).toBe(3)
  })
})
