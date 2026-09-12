import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { pill, type Tone } from '../lib/pill'
import type {
  EnrollmentCode,
  EnrollmentCreated,
  EnrollmentInput,
  EnrollmentState,
  HostSummary,
  RebootPolicy,
} from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { EnrollmentCertificates } from './EnrollmentCertificates'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const POLL_MS = 30_000

const field =
  'rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'
const primaryBtn =
  'rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500'
const ghostBtn =
  'rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50'

const STATE_TONE: Record<EnrollmentState, Tone> = {
  pending: 'info',
  consumed: 'ok',
  expired: 'neutral',
  revoked: 'danger',
}

type StateFilter = 'all' | EnrollmentState
type EnrollmentMode = 'new' | 'existing'

export function parseEnrollmentTags(text: string): Record<string, string> {
  const tags: Record<string, string> = {}
  for (const raw of text.split(',')) {
    const item = raw.trim()
    if (!item) continue
    const separator = item.indexOf('=')
    if (separator < 1) throw new Error('Tags must use key=value, separated by commas.')
    const key = item.slice(0, separator).trim()
    const value = item.slice(separator + 1).trim()
    if (!key) throw new Error('Tag keys cannot be empty.')
    tags[key] = value
  }
  return tags
}

export function EnrollmentView({ hosts }: { hosts: HostSummary[] }) {
  const [rows, setRows] = useState<EnrollmentCode[] | null>(null)
  const [stateFilter, setStateFilter] = useState<StateFilter>('all')
  const loadAction = useCallback(
    (key: string) => api.listEnrollments(key, stateFilter === 'all' ? undefined : stateFilter),
    [stateFilter],
  )
  const loader = useAdminKeyAction(loadAction)
  const runLoad = loader.run

  const acceptRows = useCallback((data: unknown) => {
    if (Array.isArray(data)) setRows(data as EnrollmentCode[])
  }, [])
  const refresh = useCallback(async () => {
    const result = await runLoad()
    if (result?.ok) acceptRows(result.data)
  }, [acceptRows, runLoad])

  useEffect(() => {
    setRows(null)
    void refresh()
    const timer = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  const hostnames = useMemo(() => new Map(hosts.map((host) => [host.id, host.hostname])), [hosts])

  return (
    <div className="space-y-4 overflow-auto p-6">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm uppercase tracking-widest text-zinc-400">Enrollment</h2>
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loader.busy}
          className={ghostBtn}
        >
          {loader.busy ? 'refreshing…' : 'refresh'}
        </button>
      </div>
      <p className="max-w-3xl text-xs text-zinc-500">
        Generate a short-lived, single-use code, then copy it manually to the target host. The code
        carries both the enrollment secret and the exact server CA fingerprint. It is never
        retrievable after this screen is dismissed.
      </p>

      <CreateEnrollmentForm hosts={hosts} onCreated={() => void refresh()} />

      <AdminActionFeedback
        actions={loader}
        onKeyAccepted={(result) => {
          if (result?.ok) acceptRows(result.data)
        }}
      />

      <section className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-xs uppercase tracking-wide text-zinc-600">Enrollment codes</h3>
          <select
            value={stateFilter}
            onChange={(event) => setStateFilter(event.target.value as StateFilter)}
            aria-label="Enrollment state"
            className={field}
          >
            <option value="all">all states</option>
            <option value="pending">pending</option>
            <option value="consumed">consumed</option>
            <option value="expired">expired</option>
            <option value="revoked">revoked</option>
          </select>
        </div>

        {rows === null ? (
          <p className="text-sm text-zinc-600">Enter the admin key to load enrollment codes.</p>
        ) : rows.length === 0 ? (
          <p className="text-sm text-zinc-600">No enrollment codes in this state.</p>
        ) : (
          <ul className="space-y-2">
            {rows.map((row) => (
              <li key={row.id}>
                <EnrollmentRow
                  enrollment={row}
                  hostname={
                    (row.target_host_id && hostnames.get(row.target_host_id)) ||
                    (row.enrolled_host_id && hostnames.get(row.enrolled_host_id)) ||
                    row.expected_hostname ||
                    'unknown host'
                  }
                  onChanged={refresh}
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      {rows !== null && <EnrollmentCertificates hosts={hosts} />}
    </div>
  )
}

function CreateEnrollmentForm({
  hosts,
  onCreated,
}: {
  hosts: HostSummary[]
  onCreated: () => void
}) {
  const [mode, setMode] = useState<EnrollmentMode>('new')
  const [hostname, setHostname] = useState('')
  const [hostId, setHostId] = useState('')
  const [label, setLabel] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [rebootPolicy, setRebootPolicy] = useState<RebootPolicy>('never')
  const [ttlMinutes, setTtlMinutes] = useState(30)
  const [created, setCreated] = useState<EnrollmentCreated | null>(null)
  const toast = useToast()

  const input = (): EnrollmentInput => {
    const common = { ttl_minutes: ttlMinutes, label: label.trim() || null }
    if (mode === 'existing') return { ...common, target_host_id: hostId }
    return {
      ...common,
      expected_hostname: hostname.trim(),
      description: description.trim() || null,
      tags: parseEnrollmentTags(tags),
      reboot_policy: rebootPolicy,
    }
  }

  const creator = useAdminKeyAction((key) => api.createEnrollment(key, input()))

  const acceptCreated = (value: unknown) => {
    if (!value || typeof value !== 'object' || !('code' in value)) return
    setCreated(value as EnrollmentCreated)
    setLabel('')
    toast.notify('success', 'Enrollment code created.')
    onCreated()
  }

  const submit = async () => {
    try {
      input()
    } catch (error) {
      creator.setError(error instanceof Error ? error.message : String(error))
      return
    }
    const result = await creator.run()
    if (result?.ok) acceptCreated(result.data)
  }

  const copyCode = async () => {
    if (!created) return
    try {
      await navigator.clipboard.writeText(created.code)
      toast.notify('success', 'Enrollment code copied.')
    } catch {
      toast.notify('error', 'Could not copy the enrollment code.')
    }
  }

  const activeHosts = hosts.filter((host) => host.is_active)
  const canSubmit =
    !creator.busy &&
    ((mode === 'new' && hostname.trim().length > 0) || (mode === 'existing' && hostId !== ''))

  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-3">
      <h3 className="text-xs uppercase tracking-wide text-zinc-600">Generate a code</h3>

      {created && (
        <div className="mt-3 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-xs">
          <p className="font-medium text-amber-300">Copy this code now. It is shown only once.</p>
          <code className="mt-2 block break-all rounded bg-zinc-950 p-2 text-zinc-100">
            {created.code}
          </code>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button type="button" onClick={() => void copyCode()} className={primaryBtn}>
              copy code
            </button>
            <button type="button" onClick={() => setCreated(null)} className={ghostBtn}>
              dismiss
            </button>
            <span className="text-zinc-500">
              expires <RelativeTime iso={created.expires_at} />
            </span>
          </div>
          <ol className="mt-3 list-decimal space-y-1 pl-4 text-zinc-400">
            <li>
              Copy <code>scripts/agent-bootstrap.sh</code> to the host over an authenticated channel
              such as SSH. Never download it over HTTP.
            </li>
            <li>
              Run{' '}
              <code className="break-all text-zinc-200">
                sudo CADENCE_DASHBOARD_URL={window.location.origin} ./agent-bootstrap.sh
              </code>
            </li>
            <li>Paste the code only at the interactive prompt.</li>
          </ol>
        </div>
      )}

      <div className="mt-3 flex flex-col gap-2">
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-1.5 text-xs text-zinc-400">
            <input
              type="radio"
              name="enrollment-mode"
              checked={mode === 'new'}
              onChange={() => setMode('new')}
              className="accent-zinc-400"
            />
            new host
          </label>
          <label className="flex items-center gap-1.5 text-xs text-zinc-400">
            <input
              type="radio"
              name="enrollment-mode"
              checked={mode === 'existing'}
              onChange={() => setMode('existing')}
              className="accent-zinc-400"
            />
            migrate existing host
          </label>
        </div>

        {mode === 'new' ? (
          <>
            <input
              type="text"
              value={hostname}
              onChange={(event) => setHostname(event.target.value)}
              placeholder="hostname, e.g. vm-web-01"
              aria-label="Expected hostname"
              className={`${field} font-mono`}
            />
            <input
              type="text"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="description (optional)"
              aria-label="Host description"
              className={field}
            />
            <input
              type="text"
              value={tags}
              onChange={(event) => setTags(event.target.value)}
              placeholder="tags: role=web, env=lab (optional)"
              aria-label="Host tags"
              className={`${field} font-mono`}
            />
            <select
              value={rebootPolicy}
              onChange={(event) => setRebootPolicy(event.target.value as RebootPolicy)}
              aria-label="Reboot policy"
              className={field}
            >
              <option value="never">reboot: never</option>
              <option value="prompt">reboot: prompt</option>
              <option value="auto">reboot: automatic</option>
            </select>
          </>
        ) : (
          <select
            value={hostId}
            onChange={(event) => setHostId(event.target.value)}
            aria-label="Existing host"
            className={field}
          >
            <option value="">select an active host…</option>
            {activeHosts.map((host) => (
              <option key={host.id} value={host.id}>
                {host.hostname}
              </option>
            ))}
          </select>
        )}

        <div className="flex flex-wrap gap-2">
          <input
            type="text"
            value={label}
            onChange={(event) => setLabel(event.target.value)}
            placeholder="label (optional)"
            aria-label="Enrollment label"
            className={field}
          />
          <select
            value={ttlMinutes}
            onChange={(event) => setTtlMinutes(Number(event.target.value))}
            aria-label="Enrollment lifetime"
            className={field}
          >
            {[5, 15, 30, 60, 120, 240].map((minutes) => (
              <option key={minutes} value={minutes}>
                expires in{' '}
                {minutes < 60
                  ? `${minutes} minutes`
                  : `${minutes / 60} ${minutes === 60 ? 'hour' : 'hours'}`}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className={primaryBtn}
          >
            {creator.busy ? 'generating…' : 'generate code'}
          </button>
        </div>
      </div>

      <AdminActionFeedback
        actions={creator}
        onKeyAccepted={(result) => {
          if (result?.ok) acceptCreated(result.data)
        }}
      />
    </section>
  )
}

function EnrollmentRow({
  enrollment,
  hostname,
  onChanged,
}: {
  enrollment: EnrollmentCode
  hostname: string
  onChanged: () => void
}) {
  const confirm = useConfirm()
  const toast = useToast()
  const revoker = useAdminKeyAction((key) => api.revokeEnrollment(enrollment.id, key))

  const acceptRevoked = () => {
    toast.notify('success', 'Enrollment code revoked.')
    onChanged()
  }

  const revoke = async () => {
    if (
      !(await confirm({
        title: `Revoke the code for ${hostname}?`,
        body: 'The code will stop working immediately. A consumed code cannot be revoked.',
        confirmLabel: 'Revoke code',
        danger: true,
      }))
    )
      return
    const result = await revoker.run()
    if (result?.ok) acceptRevoked()
  }

  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/30 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-medium text-zinc-200">{hostname}</span>
          <span className={pill(STATE_TONE[enrollment.state])}>{enrollment.state}</span>
          {enrollment.target_host_id && <span className="text-zinc-600">existing host</span>}
        </div>
        {enrollment.state === 'pending' && (
          <button
            type="button"
            onClick={() => void revoke()}
            disabled={revoker.busy}
            className={ghostBtn}
          >
            {revoker.busy ? 'revoking…' : 'revoke'}
          </button>
        )}
      </div>
      <div className="mt-1 space-y-0.5 text-zinc-500">
        {enrollment.label && <p>{enrollment.label}</p>}
        {enrollment.description && <p>{enrollment.description}</p>}
        <p>
          created <RelativeTime iso={enrollment.created_at} /> · expires{' '}
          <RelativeTime iso={enrollment.expires_at} />
          {enrollment.consumed_at && (
            <>
              {' '}
              · consumed <RelativeTime iso={enrollment.consumed_at} />
            </>
          )}
        </p>
      </div>
      <AdminActionFeedback
        actions={revoker}
        onKeyAccepted={(result) => {
          if (result?.ok) acceptRevoked()
        }}
      />
    </div>
  )
}
