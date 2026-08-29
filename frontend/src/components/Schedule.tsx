import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { relativeTime } from '../lib/time'
import type { Schedule as ScheduleData, ScheduleInput, ScheduleKind } from '../types'
import { AdminKeyPrompt, useAdminKeyAction } from './AdminKeyPrompt'

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
type RebootChoice = 'inherit' | 'auto' | 'never'

interface Form {
  kind: ScheduleKind
  day_of_month: number
  weekday: number
  hour: number
  minute: number
  timezone: string
  reboot: RebootChoice
  enabled: boolean
}

const DEFAULT_FORM: Form = {
  kind: 'weekly',
  day_of_month: 1,
  weekday: 6,
  hour: 4,
  minute: 0,
  timezone: 'UTC',
  reboot: 'inherit',
  enabled: true,
}

function toForm(s: ScheduleData): Form {
  const r = s.params?.reboot
  return {
    kind: s.kind,
    day_of_month: s.day_of_month ?? 1,
    weekday: s.weekday ?? 6,
    hour: s.hour,
    minute: s.minute,
    timezone: s.timezone,
    reboot: r === 'auto' || r === 'never' ? r : 'inherit',
    enabled: s.enabled,
  }
}

function toInput(f: Form): ScheduleInput {
  return {
    enabled: f.enabled,
    kind: f.kind,
    day_of_month: f.kind === 'monthly' ? f.day_of_month : null,
    weekday: f.kind === 'weekly' ? f.weekday : null,
    hour: f.hour,
    minute: f.minute,
    timezone: f.timezone.trim() || 'UTC',
    params: f.reboot === 'inherit' ? {} : { reboot: f.reboot },
  }
}

const num = (v: string, lo: number, hi: number, fb: number) => {
  const n = Number(v)
  return Number.isInteger(n) && n >= lo && n <= hi ? n : fb
}

export function Schedule({ hostId }: { hostId: string }) {
  const [existing, setExisting] = useState<ScheduleData | null>(null)
  const [form, setForm] = useState<Form>(DEFAULT_FORM)
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const list = await api.getSchedules(hostId)
      const s = list[0] ?? null
      setExisting(s)
      setForm(s ? toForm(s) : DEFAULT_FORM)
    } catch {
      /* transient */
    } finally {
      setLoaded(true)
    }
  }, [hostId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const save = useCallback(
    (key: string) =>
      existing
        ? api.updateSchedule(existing.id, key, toInput(form))
        : api.createSchedule(hostId, key, toInput(form)),
    [existing, hostId, form],
  )
  const del = useCallback(
    (key: string) => api.deleteSchedule(existing!.id, key),
    [existing],
  )

  const saver = useAdminKeyAction(save)
  const deleter = useAdminKeyAction(del)

  const onSave = useCallback(async () => {
    const r = await saver.run()
    if (r?.ok) await refresh()
  }, [saver, refresh])
  const onDelete = useCallback(async () => {
    const r = await deleter.run()
    if (r?.ok) await refresh()
  }, [deleter, refresh])
  const onKeySubmit = useCallback(async () => {
    const r = saver.needKey ? await saver.submitKey() : await deleter.submitKey()
    if (r?.ok) await refresh()
  }, [saver, deleter, refresh])

  const set = <K extends keyof Form>(k: K, v: Form[K]) => setForm((f) => ({ ...f, [k]: v }))
  const field = 'rounded border border-zinc-700 bg-zinc-900 px-1 py-0.5 text-xs text-zinc-200 outline-none focus:border-zinc-500'

  return (
    <section className="border-t border-zinc-800 px-6 py-4">
      <div className="flex items-center justify-between">
        <h3 className="text-xs uppercase tracking-wide text-zinc-600">Schedule</h3>
        {loaded && (
          <span className="text-xs text-zinc-600">
            {existing?.enabled && existing.next_run_at
              ? `next run ${relativeTime(existing.next_run_at)}`
              : existing
                ? 'disabled'
                : 'none'}
          </span>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-zinc-400">
        <select value={form.kind} onChange={(e) => set('kind', e.target.value as ScheduleKind)} className={field}>
          <option value="weekly">weekly</option>
          <option value="monthly">monthly</option>
        </select>

        {form.kind === 'weekly' ? (
          <select value={form.weekday} onChange={(e) => set('weekday', Number(e.target.value))} className={field}>
            {WEEKDAYS.map((d, i) => (
              <option key={d} value={i}>{d}</option>
            ))}
          </select>
        ) : (
          <label className="flex items-center gap-1">
            day
            <input
              type="number" min={1} max={28} value={form.day_of_month}
              onChange={(e) => set('day_of_month', num(e.target.value, 1, 28, form.day_of_month))}
              className={`${field} w-14`}
            />
          </label>
        )}

        <label className="flex items-center gap-1">
          at
          <input type="number" min={0} max={23} value={form.hour}
            onChange={(e) => set('hour', num(e.target.value, 0, 23, form.hour))}
            className={`${field} w-14`} />
          :
          <input type="number" min={0} max={59} value={form.minute}
            onChange={(e) => set('minute', num(e.target.value, 0, 59, form.minute))}
            className={`${field} w-14`} />
        </label>

        <input
          type="text" value={form.timezone} onChange={(e) => set('timezone', e.target.value)}
          placeholder="UTC" size={14} className={field}
        />

        <label className="flex items-center gap-1">
          reboot
          <select value={form.reboot} onChange={(e) => set('reboot', e.target.value as RebootChoice)} className={field}>
            <option value="inherit">host default</option>
            <option value="auto">auto</option>
            <option value="never">never</option>
          </select>
        </label>

        <label className="flex items-center gap-1 select-none">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set('enabled', e.target.checked)} className="accent-zinc-400" />
          enabled
        </label>

        <button
          type="button" onClick={() => void onSave()} disabled={saver.busy}
          className="rounded bg-zinc-100 px-2 py-1 font-medium text-zinc-900 hover:bg-white disabled:bg-zinc-800 disabled:text-zinc-500"
        >
          {saver.busy ? 'saving…' : existing ? 'update' : 'create'}
        </button>
        {existing && (
          <button
            type="button" onClick={() => void onDelete()} disabled={deleter.busy}
            className="rounded border border-zinc-700 px-2 py-1 text-zinc-400 hover:text-zinc-200"
          >
            delete
          </button>
        )}
      </div>

      {(saver.needKey || deleter.needKey) && (
        <AdminKeyPrompt
          value={saver.needKey ? saver.keyDraft : deleter.keyDraft}
          onChange={saver.needKey ? saver.setKeyDraft : deleter.setKeyDraft}
          onSubmit={() => void onKeySubmit()}
        />
      )}
      {(saver.error || deleter.error) && (
        <p className="mt-2 text-xs text-red-400">{saver.error ?? deleter.error}</p>
      )}
    </section>
  )
}
