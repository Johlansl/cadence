import { pill, type Tone } from '../lib/pill'
import type { HostStatus } from '../types'

const STYLES: Record<HostStatus, { label: string; tone: Tone }> = {
  up_to_date: { label: 'up to date', tone: 'ok' },
  updates_available: { label: 'updates', tone: 'warn' },
  security_updates_available: { label: 'security', tone: 'danger' },
}

export function StatusBadge({ status }: { status: HostStatus }) {
  const s = STYLES[status]
  return <span className={pill(s.tone)}>{s.label}</span>
}
