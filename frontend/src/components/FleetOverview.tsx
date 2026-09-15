import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { attentionRank } from '../lib/hostFilter'
import { pill, TONE_TEXT } from '../lib/pill'
import { relativeTime } from '../lib/time'
import { useNow } from '../lib/useNow'
import type { FleetSummary, HostSummary } from '../types'
import { Freshness } from './Freshness'
import { HealthBadge } from './HealthBadge'
import { StatusBadge } from './StatusBadge'

type AlertTone = 'danger' | 'warn' | 'reboot'

// Attenuated alert box, used only for metrics needing action. Healthy tiles
// stay neutral: the value drops to zinc and the box keeps the quiet default.
const ALERT_BOX: Record<AlertTone, string> = {
  danger: 'border-red-500/25 bg-red-500/[0.06]',
  warn: 'border-amber-500/25 bg-amber-500/[0.07]',
  reboot: 'border-orange-500/25 bg-orange-500/[0.06]',
}

function Tile({
  label,
  value,
  sub,
  tone,
  alert,
}: {
  label: string
  value: string | number
  sub?: string
  tone?: keyof typeof TONE_TEXT
  alert?: AlertTone
}) {
  return (
    <div
      className={`rounded border px-3 py-2 ${alert ? ALERT_BOX[alert] : 'border-zinc-800/70 bg-zinc-900/40'}`}
    >
      <div className="text-xs uppercase tracking-wide text-zinc-600">{label}</div>
      <div className={`mt-0.5 text-lg tabular-nums ${tone ? TONE_TEXT[tone] : 'text-zinc-300'}`}>
        {value}
      </div>
      {sub && <div className="text-xs text-zinc-500">{sub}</div>}
    </div>
  )
}

export function FleetOverview({
  hosts,
  onSelect,
  onShowAttention,
}: {
  hosts: HostSummary[]
  onSelect: (id: string) => void
  onShowAttention: () => void
}) {
  useNow() // keep the staleness-ranked "needs attention" list moving between polls
  const [summary, setSummary] = useState<FleetSummary | null>(null)
  const [summaryError, setSummaryError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .getFleetSummary()
        .then((s) => {
          if (cancelled) return
          setSummary(s)
          setSummaryError(null)
        })
        .catch((e) => {
          // Keep the previous summary on screen so a failed refresh reads as
          // stale data, never as zero. A null summary stays unavailable.
          if (!cancelled) setSummaryError(e instanceof Error ? e.message : String(e))
        })
    void load()
    const t = setInterval(load, 30_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  // A tile driven by the fleet summary: real value (including 0) once loaded,
  // "n/a" when unavailable, "…" while the first fetch is still in flight.
  const pending = summary === null ? (summaryError ? 'n/a' : '…') : null

  const attention = hosts
    .filter((h) => h.is_active && attentionRank(h) < 99)
    .sort((a, b) => attentionRank(a) - attentionRank(b) || a.hostname.localeCompare(b.hostname))

  const s = summary
  const activeHosts = hosts.filter((h) => h.is_active)
  const unhealthy = activeHosts.filter((h) => h.health_status === 'unhealthy').length
  const degraded = activeHosts.filter((h) => h.health_status === 'degraded').length
  const unknown = activeHosts.filter((h) => h.health_status === 'unknown').length

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm font-medium uppercase tracking-widest text-zinc-300">
        Fleet overview
        {summaryError && summary && (
          <span className="ml-2 normal-case text-red-500/70">· stale</span>
        )}
      </h2>
      {summaryError && !summary && (
        <p className="text-xs text-red-400">sync error: {summaryError}</p>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-7">
        <Tile
          label="Hosts"
          value={s ? s.active_hosts : hosts.filter((h) => h.is_active).length}
          sub={s && s.inactive_hosts > 0 ? `${s.inactive_hosts} inactive` : undefined}
        />
        <Tile
          label="Security"
          value={s ? s.security_updates_available : (pending ?? 'n/a')}
          sub={
            s ? `${s.security_updates} package${s.security_updates === 1 ? '' : 's'}` : undefined
          }
          tone={s && s.security_updates_available > 0 ? 'danger' : undefined}
          alert={s && s.security_updates_available > 0 ? 'danger' : undefined}
        />
        <Tile
          label="Health"
          value={unhealthy}
          sub={`${degraded} degraded, ${unknown} unknown`}
          tone={unhealthy > 0 ? 'danger' : degraded > 0 || unknown > 0 ? 'warn' : undefined}
          alert={unhealthy > 0 ? 'danger' : degraded > 0 || unknown > 0 ? 'warn' : undefined}
        />
        <Tile
          label="Needs updates"
          value={s ? s.updates_available + s.security_updates_available : (pending ?? 'n/a')}
          sub={s ? `${s.pending_updates} package${s.pending_updates === 1 ? '' : 's'}` : undefined}
          tone={s && s.updates_available + s.security_updates_available > 0 ? 'warn' : undefined}
          alert={s && s.updates_available + s.security_updates_available > 0 ? 'warn' : undefined}
        />
        <Tile
          label="Reboot"
          value={s ? s.reboot_required : (pending ?? 'n/a')}
          tone={s && s.reboot_required > 0 ? 'reboot' : undefined}
          alert={s && s.reboot_required > 0 ? 'reboot' : undefined}
        />
        <Tile
          label="Overdue"
          value={s ? s.late + s.silent : (pending ?? 'n/a')}
          sub={
            s?.oldest_report_age_seconds != null
              ? `oldest ${relativeTime(new Date(Date.now() - s.oldest_report_age_seconds * 1000).toISOString())}`
              : undefined
          }
          tone={s && s.silent > 0 ? 'danger' : s && s.late > 0 ? 'warn' : undefined}
          alert={s && s.silent > 0 ? 'danger' : s && s.late > 0 ? 'warn' : undefined}
        />
        <Tile
          label="Jobs 24h"
          value={s ? `${s.jobs_succeeded_24h}✓ ${s.jobs_failed_24h}✕` : (pending ?? 'n/a')}
          sub={s && s.jobs_running > 0 ? `${s.jobs_running} running` : undefined}
          tone={s && s.jobs_failed_24h > 0 ? 'danger' : undefined}
          alert={s && s.jobs_failed_24h > 0 ? 'danger' : undefined}
        />
      </div>

      <div>
        <h3 className="mb-2 text-xs uppercase tracking-wide text-zinc-500">
          Needs attention ({attention.length})
        </h3>
        {attention.length === 0 ? (
          <p className="text-sm text-zinc-500">All active hosts are up to date and fresh.</p>
        ) : (
          <ul className="divide-y divide-zinc-800 rounded border border-zinc-800">
            {attention.slice(0, 12).map((h) => (
              <li key={h.id}>
                <button
                  type="button"
                  onClick={() => onSelect(h.id)}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:bg-zinc-900"
                >
                  <span className="truncate font-mono text-zinc-200">{h.hostname}</span>
                  <span className="flex shrink-0 items-center gap-2 text-xs">
                    <HealthBadge status={h.health_status} />
                    <StatusBadge status={h.status} />
                    {h.reboot_required && <span className={pill('reboot')}>reboot</span>}
                    <span className="text-zinc-600">
                      <Freshness iso={h.last_seen_at} />
                    </span>
                  </span>
                </button>
              </li>
            ))}
            {attention.length > 12 && (
              <li>
                <button
                  type="button"
                  onClick={onShowAttention}
                  className="w-full px-3 py-1.5 text-left text-xs text-zinc-500 hover:text-zinc-300"
                >
                  +{attention.length - 12} more — show all {attention.length}
                </button>
              </li>
            )}
          </ul>
        )}
      </div>
    </div>
  )
}
