import { describe, expect, it } from 'vitest'
import { relativeTime, staleness } from './time'

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()

describe('staleness', () => {
  it('buckets by age: fresh <=5m, late <=15m, stale beyond', () => {
    expect(staleness(ago(2 * 60_000))).toBe('fresh')
    expect(staleness(ago(10 * 60_000))).toBe('late')
    expect(staleness(ago(60 * 60_000))).toBe('stale')
    expect(staleness(null)).toBe('stale')
  })
})

describe('relativeTime', () => {
  it('formats recent times', () => {
    expect(relativeTime(null)).toBe('never')
    expect(relativeTime(ago(2_000))).toBe('just now')
    expect(relativeTime(ago(30_000))).toMatch(/\d+s ago/)
    expect(relativeTime(ago(5 * 60_000))).toMatch(/\d+m ago/)
    expect(relativeTime(ago(3 * 60 * 60_000))).toMatch(/\d+h ago/)
    expect(relativeTime(ago(3 * 24 * 60 * 60_000))).toMatch(/\d+d ago/)
  })
})
