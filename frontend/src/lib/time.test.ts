import { describe, expect, it } from 'vitest'
import { relativeTime, staleness } from './time'

const ago = (ms: number) => new Date(Date.now() - ms).toISOString()
const ahead = (ms: number) => new Date(Date.now() + ms).toISOString()

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

  it('formats future times with an "in" prefix', () => {
    expect(relativeTime(ahead(30_000))).toMatch(/^in \d+s$/)
    expect(relativeTime(ahead(5 * 60_000))).toMatch(/^in \d+m$/)
    expect(relativeTime(ahead(3 * 60 * 60_000))).toMatch(/^in \d+h$/)
    expect(relativeTime(ahead(2 * 24 * 60 * 60_000))).toMatch(/^in \d+d$/)
  })
})
