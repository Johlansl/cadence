import { useEffect, useReducer } from 'react'

// Re-render the calling component on a fixed interval, so wall-clock-relative
// output (relativeTime / staleness) keeps moving between data polls. Each caller
// owns a cheap timer -- same pattern as the poll loops in FleetOverview / Jobs /
// HostHistory. Replaces the single 15s re-render of the whole App tree.
export function useNow(ms = 15_000): void {
  const [, bump] = useReducer((n: number) => n + 1, 0)
  useEffect(() => {
    const t = setInterval(bump, ms)
    return () => clearInterval(t)
  }, [ms])
}
