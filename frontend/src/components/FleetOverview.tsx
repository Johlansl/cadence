import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { TONE_TEXT } from '../lib/pill'
import { relativeTime, staleness } from '../lib/time'
import type { FleetSummary, HostSummary } from '../types'
import { Freshness } from './Freshness'
import { StatusBadge } from './StatusBadge'

function Tile({
  label,
  value,
  sub,
  tone,
}: {
  label: string
  value: string | number
  sub?: string
  tone?: keyof typeof TONE_TEXT
}) {
  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/40 px-3 py-2">
      <div className="text-xs uppercase tracking-wide text-zinc-600">{label}</div>
      <div className={`mt-0.5 text-lg tabular-nums ${tone ? TONE_TEXT[tone] : 'text-zinc-100'}`}>
        {value}
      </div>
      {sub && <div className="text-xs text-zinc-500">{sub}</div>}
    </div>
  )
}

// Highest concern first: security > reboot pending > plain updates > overdue.
function attentionRank(h: HostSummary): number {
  if (h.status === 'security_updates_available') return 0
  if (h.reboot_required) return 1
  if (h.status === 'updates_available') return 2
  if (staleness(h.last_seen_at) !== 'fresh') return 3
  return 99
}

export function FleetOverview({
  hosts,
  onSelect,
}: {
  hosts: HostSummary[]
  onSelect: (id: string) => void
}) {
  const [summary, setSummary] = useState<FleetSummary | null>(null)

  useEffect(() => {
    let cancelled = false
    const load = () =>
      api
        .getFleetSummary()
        .then((s) => !cancelled && setSummary(s))
        .catch(() => {})
    void load()
    const t = setInterval(load, 30_000)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  const attention = hosts
    .filter((h) => h.is_active && attentionRank(h) < 99)
    .sort((a, b) => attentionRank(a) - attentionRank(b) || a.hostname.localeCompare(b.hostname))

  const s = summary

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm uppercase tracking-widest text-zinc-400">Fleet overview</h2>

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
        <Tile
          label="Hosts"
          value={s ? s.active_hosts : hosts.filter((h) => h.is_active).length}
          sub={s && s.inactive_hosts > 0 ? `${s.inactive_hosts} inactive` : undefined}
        />
        <Tile
          label="Security"
          value={s ? s.security_updates_available : '—'}
          sub={
            s ? `${s.security_updates} package${s.security_updates === 1 ? '' : 's'}` : undefined
          }
          tone={s && s.security_updates_available > 0 ? 'danger' : undefined}
        />
        <Tile
          label="Needs updates"
          value={s ? s.updates_available + s.security_updates_available : '—'}
          sub={s ? `${s.pending_updates} package${s.pending_updates === 1 ? '' : 's'}` : undefined}
          tone={s && s.updates_available + s.security_updates_available > 0 ? 'warn' : undefined}
        />
        <Tile label="Reboot" value={s ? s.reboot_required : '—'} tone="reboot" />
        <Tile
          label="Overdue"
          value={s ? s.late + s.silent : '—'}
          sub={
            s?.oldest_report_age_seconds != null
              ? `oldest ${relativeTime(new Date(Date.now() - s.oldest_report_age_seconds * 1000).toISOString())}`
              : undefined
          }
          tone={s && s.silent > 0 ? 'danger' : s && s.late > 0 ? 'warn' : undefined}
        />
        <Tile
          label="Jobs 24h"
          value={s ? `${s.jobs_succeeded_24h}✓ ${s.jobs_failed_24h}✕` : '—'}
          sub={s && s.jobs_running > 0 ? `${s.jobs_running} running` : undefined}
          tone={s && s.jobs_failed_24h > 0 ? 'danger' : undefined}
        />
      </div>

      <div>
        <h3 className="mb-2 text-xs uppercase tracking-wide text-zinc-600">
          Needs attention ({attention.length})
        </h3>
        {attention.length === 0 ? (
          <p className="text-sm text-emerald-500">All active hosts are up to date and fresh.</p>
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
                    <StatusBadge status={h.status} />
                    {h.reboot_required && <span className={TONE_TEXT.reboot}>reboot</span>}
                    <span className="text-zinc-600">
                      <Freshness iso={h.last_seen_at} />
                    </span>
                  </span>
                </button>
              </li>
            ))}
            {attention.length > 12 && (
              <li className="px-3 py-1.5 text-xs text-zinc-600">+{attention.length - 12} more</li>
            )}
          </ul>
        )}
      </div>
    </div>
  )
}
