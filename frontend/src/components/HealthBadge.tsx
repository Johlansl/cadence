import { pill, type Tone } from '../lib/pill'
import type { HostHealthStatus } from '../types'

const STYLES: Record<HostHealthStatus, Tone> = {
  healthy: 'ok',
  degraded: 'warn',
  unhealthy: 'danger',
  unknown: 'neutral',
}

export function HealthBadge({
  status,
  includeLabel = false,
}: {
  status: HostHealthStatus
  includeLabel?: boolean
}) {
  return (
    <span className={pill(STYLES[status])} title={`Host health: ${status}`}>
      {includeLabel ? `health: ${status}` : status}
    </span>
  )
}
