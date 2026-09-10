import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { pill, type Tone } from '../lib/pill'
import type {
  Campaign,
  CampaignDetail,
  CampaignHostState,
  CampaignStatus,
  HostSummary,
  StageSize,
} from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const POLL_MS = 30_000

const field =
  'rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'
const primaryBtn =
  'rounded bg-zinc-100 px-2 py-1 text-xs font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500'
const ghostBtn =
  'rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50'

const STATUS_TONE: Record<CampaignStatus, Tone> = {
  draft: 'neutral',
  running: 'info',
  paused: 'warn',
  completed: 'ok',
  stopped: 'danger',
  cancelled: 'neutral',
}
const HOST_TONE: Record<CampaignHostState, Tone> = {
  pending: 'neutral',
  running: 'info',
  done: 'ok',
  skipped: 'warn',
  orphaned: 'neutral',
}
const TERMINAL: CampaignStatus[] = ['completed', 'stopped', 'cancelled']

// "2, 25%, rest" -> [2, "25%", "rest"]. Bare integers become numbers; anything
// else is passed through and the server validates it.
export function parseStages(text: string): StageSize[] {
  return text
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => (/^\d+$/.test(s) ? Number(s) : s))
}

function progress(c: Campaign): string {
  const parts = [`${c.hosts_done}/${c.hosts_total} done`]
  if (c.hosts_skipped) parts.push(`${c.hosts_skipped} skipped`)
  if (c.hosts_orphaned) parts.push(`${c.hosts_orphaned} orphaned`)
  const stage =
    c.current_stage_index == null
      ? c.status === 'draft'
        ? 'not started'
        : 'no active stage'
      : `stage ${c.current_stage_index + 1}/${c.stages.length}`
  return `${stage} · ${parts.join(' · ')}`
}

export function CampaignsView() {
  const [rows, setRows] = useState<Campaign[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setRows(await api.listCampaigns())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  return (
    <div className="space-y-4 overflow-auto p-6">
      <h2 className="text-sm uppercase tracking-widest text-zinc-400">Campaigns</h2>
      <p className="max-w-2xl text-xs text-zinc-500">
        A staged rollout of dist-upgrades across a fixed host set: canary waves, a global
        concurrency cap, and an automatic stop when failures pile up. Created paused as a{' '}
        <span className="font-mono text-[11px]">draft</span>; press activate to start it.
      </p>

      <CreateForm onCreated={refresh} />

      {error && <p className="text-xs text-red-400">sync error: {error}</p>}

      {rows === null ? (
        <p className="text-sm text-zinc-600">loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-zinc-600">No campaigns yet.</p>
      ) : (
        <ul className="space-y-3">
          {rows.map((c) => (
            <li key={c.id}>
              <CampaignRow campaign={c} onChanged={refresh} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CampaignRow({ campaign, onChanged }: { campaign: Campaign; onChanged: () => void }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState<CampaignDetail | null>(null)
  const toast = useToast()
  const confirm = useConfirm()

  const loadDetail = useCallback(async () => {
    try {
      setDetail(await api.getCampaign(campaign.id))
    } catch {
      setDetail(null)
    }
  }, [campaign.id])

  useEffect(() => {
    if (!open) return
    void loadDetail()
    const t = setInterval(() => void loadDetail(), POLL_MS)
    return () => clearInterval(t)
  }, [open, loadDetail])

  const activate = useAdminKeyAction((key) => api.campaignAction(campaign.id, key, 'activate'))
  const pause = useAdminKeyAction((key) => api.campaignAction(campaign.id, key, 'pause'))
  const resume = useAdminKeyAction((key) => api.campaignAction(campaign.id, key, 'resume'))
  const cancel = useAdminKeyAction((key) => api.campaignAction(campaign.id, key, 'cancel'))

  const run = async (a: typeof activate, label: string, ok = true) => {
    if (
      !ok &&
      !(await confirm({
        title: `Cancel campaign "${campaign.name}"?`,
        body: 'No further jobs are created. Jobs already running finish on their hosts; those hosts are marked orphaned.',
        confirmLabel: 'Cancel campaign',
        danger: true,
      }))
    )
      return
    const r = await a.run()
    if (r?.ok) {
      toast.notify('success', `Campaign ${label}.`)
      onChanged()
      if (open) void loadDetail()
    }
  }

  const s = campaign.status

  return (
    <div className="rounded border border-zinc-800">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800 bg-zinc-900/40 px-3 py-1.5">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-2 text-left"
        >
          <span className="text-zinc-500">{open ? '▾' : '▸'}</span>
          <span className="text-xs font-medium text-zinc-200">{campaign.name}</span>
          <span className={pill(STATUS_TONE[s])}>{s}</span>
        </button>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-zinc-500">{progress(campaign)}</span>
          {s === 'draft' && (
            <button
              type="button"
              onClick={() => void run(activate, 'activated')}
              disabled={activate.busy}
              className={primaryBtn}
            >
              activate
            </button>
          )}
          {s === 'running' && (
            <button
              type="button"
              onClick={() => void run(pause, 'paused')}
              disabled={pause.busy}
              className={ghostBtn}
            >
              pause
            </button>
          )}
          {s === 'paused' && (
            <button
              type="button"
              onClick={() => void run(resume, 'resumed')}
              disabled={resume.busy}
              className={ghostBtn}
            >
              resume
            </button>
          )}
          {!TERMINAL.includes(s) && (
            <button
              type="button"
              onClick={() => void run(cancel, 'cancelled', false)}
              disabled={cancel.busy}
              className={ghostBtn}
            >
              cancel
            </button>
          )}
        </div>
      </div>

      <div className="space-y-2 px-3 py-2 text-xs text-zinc-500">
        <p>
          {campaign.stages.length} stage{campaign.stages.length === 1 ? '' : 's'} ·{' '}
          <span className="font-mono text-[11px]">[{campaign.stages.join(', ')}]</span> ·
          concurrency {campaign.max_concurrency} · max failures {campaign.max_failures} · window{' '}
          {campaign.observation_window_seconds}s
        </p>
        {campaign.halt_reason && <p className="text-red-400">stopped: {campaign.halt_reason}</p>}
        <p>
          created <RelativeTime iso={campaign.created_at} />
          {campaign.started_at && (
            <>
              {' '}
              · started <RelativeTime iso={campaign.started_at} />
            </>
          )}
          {campaign.completed_at && (
            <>
              {' '}
              · ended <RelativeTime iso={campaign.completed_at} />
            </>
          )}
        </p>

        {open &&
          (detail === null ? (
            <p className="text-zinc-600">loading detail…</p>
          ) : (
            <div className="space-y-3 pt-1">
              <StageBreakdown detail={detail} />
              <HostTable detail={detail} />
            </div>
          ))}
      </div>

      <div className="px-3 pb-2">
        <AdminActionFeedback actions={[activate, pause, resume, cancel]} />
      </div>
    </div>
  )
}

function StageBreakdown({ detail }: { detail: CampaignDetail }) {
  return (
    <div className="flex flex-wrap gap-2">
      {detail.stages_detail.map((st) => (
        <div key={st.index} className="rounded border border-zinc-800 px-2 py-1">
          <span className="text-zinc-400">
            stage {st.index + 1}{' '}
            <span className="font-mono text-[10px]">({String(st.size_spec)})</span>
          </span>
          <span className="ml-2 text-zinc-500">
            {st.done}/{st.hosts_total} done
            {st.skipped > 0 && ` · ${st.skipped} skipped`}
            {st.orphaned > 0 && ` · ${st.orphaned} orphaned`}
            {st.running > 0 && ` · ${st.running} running`}
          </span>
        </div>
      ))}
    </div>
  )
}

function HostTable({ detail }: { detail: CampaignDetail }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-[11px]">
        <thead className="text-zinc-600">
          <tr>
            <th className="py-1 pr-3 font-normal">host</th>
            <th className="py-1 pr-3 font-normal">stage</th>
            <th className="py-1 pr-3 font-normal">state</th>
            <th className="py-1 pr-3 font-normal">reason</th>
            <th className="py-1 font-normal">job</th>
          </tr>
        </thead>
        <tbody className="text-zinc-400">
          {detail.hosts.map((h) => (
            <tr key={h.host_id} className="border-t border-zinc-800/60">
              <td className="py-1 pr-3 font-mono">{h.hostname}</td>
              <td className="py-1 pr-3">{h.stage_index + 1}</td>
              <td className="py-1 pr-3">
                <span className={pill(HOST_TONE[h.state])}>{h.state}</span>
              </td>
              <td className="py-1 pr-3">{h.skip_reason ?? ''}</td>
              <td className="py-1 font-mono text-zinc-600">
                {h.job_id ? h.job_id.slice(0, 8) : ''}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function CreateForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState('')
  const [stages, setStages] = useState('1, rest')
  const [maxConcurrency, setMaxConcurrency] = useState('1')
  const [maxFailures, setMaxFailures] = useState('0')
  const [window, setWindow] = useState('')
  const [mode, setMode] = useState<'hosts' | 'tag'>('tag')
  const [tag, setTag] = useState('')
  const [hostIds, setHostIds] = useState<string[]>([])
  const [hosts, setHosts] = useState<HostSummary[]>([])
  const toast = useToast()

  useEffect(() => {
    void api.listHosts().then(setHosts)
  }, [])

  const creator = useAdminKeyAction((key) =>
    api.createCampaign(key, {
      name: name.trim(),
      stages: parseStages(stages),
      max_concurrency: Number(maxConcurrency),
      max_failures: Number(maxFailures),
      observation_window_seconds: window.trim() === '' ? null : Number(window),
      ...(mode === 'hosts' ? { host_ids: hostIds } : { tag: tag.trim() }),
    }),
  )

  const submit = async () => {
    const r = await creator.run()
    if (r?.ok) {
      setName('')
      setHostIds([])
      setTag('')
      toast.notify('success', 'Campaign created as a draft.')
      onCreated()
    }
  }

  const toggleHost = (id: string) =>
    setHostIds((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]))

  const canSubmit =
    name.trim().length > 0 &&
    parseStages(stages).length > 0 &&
    (mode === 'tag' ? tag.trim().length > 0 : hostIds.length > 0) &&
    !creator.busy

  return (
    <section className="rounded border border-zinc-800 bg-zinc-900/40 p-3">
      <h3 className="text-xs uppercase tracking-wide text-zinc-600">New campaign</h3>

      <div className="mt-2 flex flex-col gap-2">
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="name"
          aria-label="Campaign name"
          className={field}
        />
        <div className="flex flex-wrap gap-2">
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            stages
            <input
              type="text"
              value={stages}
              onChange={(e) => setStages(e.target.value)}
              placeholder="1, 25%, rest"
              aria-label="Stages"
              className={`${field} w-40 font-mono`}
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            concurrency
            <input
              type="number"
              min={1}
              value={maxConcurrency}
              onChange={(e) => setMaxConcurrency(e.target.value)}
              aria-label="Max concurrency"
              className={`${field} w-16`}
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            max failures
            <input
              type="number"
              min={0}
              value={maxFailures}
              onChange={(e) => setMaxFailures(e.target.value)}
              aria-label="Max failures"
              className={`${field} w-16`}
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            window s
            <input
              type="number"
              min={0}
              value={window}
              onChange={(e) => setWindow(e.target.value)}
              placeholder="600"
              aria-label="Observation window seconds"
              className={`${field} w-20`}
            />
          </label>
        </div>

        <fieldset className="flex flex-col gap-1.5">
          <legend className="text-xs text-zinc-600">target</legend>
          <div className="flex gap-4 text-xs text-zinc-400">
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="cx-target"
                checked={mode === 'tag'}
                onChange={() => setMode('tag')}
                className="accent-zinc-400"
              />
              by tag
            </label>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name="cx-target"
                checked={mode === 'hosts'}
                onChange={() => setMode('hosts')}
                className="accent-zinc-400"
              />
              pick hosts
            </label>
          </div>
          {mode === 'tag' ? (
            <input
              type="text"
              value={tag}
              onChange={(e) => setTag(e.target.value)}
              placeholder='"role=web" or "web"'
              aria-label="Tag filter"
              className={`${field} font-mono`}
            />
          ) : (
            <div className="flex max-h-40 flex-wrap gap-x-4 gap-y-1 overflow-auto rounded border border-zinc-800 p-2">
              {hosts.map((h) => (
                <label
                  key={h.id}
                  className="flex select-none items-center gap-1 text-xs text-zinc-400"
                >
                  <input
                    type="checkbox"
                    checked={hostIds.includes(h.id)}
                    onChange={() => toggleHost(h.id)}
                    className="accent-zinc-400"
                  />
                  <span className="font-mono text-[11px]">{h.hostname}</span>
                </label>
              ))}
            </div>
          )}
        </fieldset>

        <div>
          <button
            type="button"
            onClick={() => void submit()}
            disabled={!canSubmit}
            className={primaryBtn}
          >
            {creator.busy ? 'creating…' : 'create draft'}
          </button>
        </div>
      </div>

      <AdminActionFeedback actions={creator} />
    </section>
  )
}
