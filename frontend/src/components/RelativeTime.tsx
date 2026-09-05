import { useNow } from '../lib/useNow'
import { relativeTime } from '../lib/time'

// A "3m ago" label that updates itself on an interval, without re-rendering
// anything above it.
export function RelativeTime({ iso }: { iso: string | null }) {
  useNow()
  return <>{relativeTime(iso)}</>
}
