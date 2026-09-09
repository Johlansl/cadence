import type {
  FleetSummary,
  HostDetail,
  HostSummary,
  Job,
  PackageStatusFilter,
  PackageSummaryRow,
  RebootPolicy,
  ReportSummary,
  Schedule,
  ScheduleInput,
  Webhook,
  WebhookCreated,
  WebhookInput,
} from '../types'

const BASE = '/api/v1'

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

// Result of an admin write (X-Admin-Key). `ok` distinguishes the 2xx path;
// `status` and `detail` carry the failure (401 invalid key, 409 conflict, ...).
export interface AdminWriteResult<T> {
  ok: boolean
  status: number
  data?: T
  detail?: string
}

// FastAPI returns a string `detail` for our own HTTPExceptions, but a list of
// {type, loc, msg, ...} objects for 422 request-validation errors. Flatten it to
// a readable string so callers can render it straight into the DOM.
function errorDetail(body: unknown, status: number): string {
  const d = body && typeof body === 'object' ? (body as { detail?: unknown }).detail : undefined
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d)) {
    const msgs = d.flatMap((e) => {
      if (!e || typeof e !== 'object' || !('msg' in e)) return []
      const rawLoc = (e as { loc?: unknown }).loc
      const loc = Array.isArray(rawLoc) ? rawLoc.filter((p) => p !== 'body').join('.') : ''
      const msg = String((e as { msg: unknown }).msg)
      return [loc ? `${loc}: ${msg}` : msg]
    })
    if (msgs.length) return msgs.join('; ')
  }
  return `Request failed (${status}).`
}

async function adminWrite<T>(
  path: string,
  adminKey: string,
  method: 'POST' | 'PATCH' | 'DELETE',
  body: unknown,
): Promise<AdminWriteResult<T>> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: {
      'Content-Type': 'application/json',
      Accept: 'application/json',
      'X-Admin-Key': adminKey,
    },
    body: JSON.stringify(body),
  })
  if (res.ok) {
    if (res.status === 204) return { ok: true, status: 204 }
    return { ok: true, status: res.status, data: (await res.json()) as T }
  }
  let detail: string | undefined
  try {
    detail = errorDetail(await res.json(), res.status)
  } catch {
    /* no JSON body -- caller falls back to a generic message */
  }
  return { ok: false, status: res.status, detail }
}

export const api = {
  listHosts: () => getJSON<HostSummary[]>('/hosts'),
  getHost: (id: string) => getJSON<HostDetail>(`/hosts/${id}`),
  getFleetSummary: () => getJSON<FleetSummary>('/fleet/summary'),

  listPackages: (
    opts: {
      name?: string
      status?: PackageStatusFilter
      limit?: number
      after?: string
      afterId?: string
    } = {},
  ) => {
    const p = new URLSearchParams()
    if (opts.name) p.set('name', opts.name)
    if (opts.status) p.set('status', opts.status)
    if (opts.limit) p.set('limit', String(opts.limit))
    if (opts.after) p.set('after', opts.after)
    if (opts.afterId) p.set('after_id', opts.afterId)
    const qs = p.toString()
    return getJSON<PackageSummaryRow[]>(`/packages${qs ? `?${qs}` : ''}`)
  },

  getHostJobs: (id: string, opts: { limit?: number; before?: string } = {}) => {
    const p = new URLSearchParams()
    if (opts.limit) p.set('limit', String(opts.limit))
    if (opts.before) p.set('before', opts.before)
    const qs = p.toString()
    return getJSON<Job[]>(`/hosts/${id}/jobs${qs ? `?${qs}` : ''}`)
  },

  getHostReports: (id: string, opts: { limit?: number; before?: string } = {}) => {
    const p = new URLSearchParams()
    if (opts.limit) p.set('limit', String(opts.limit))
    if (opts.before) p.set('before', opts.before)
    const qs = p.toString()
    return getJSON<ReportSummary[]>(`/hosts/${id}/reports${qs ? `?${qs}` : ''}`)
  },

  // reboot: omit to use the host's reboot_policy; set to override for this job.
  createJob(hostId: string, adminKey: string, reboot?: RebootPolicy) {
    const body: Record<string, unknown> = { requested_by: 'dashboard' }
    if (reboot) body.params = { reboot }
    return adminWrite<Job>(`/admin/hosts/${hostId}/jobs`, adminKey, 'POST', body)
  },

  clearHostJobs(hostId: string, adminKey: string) {
    return adminWrite<null>(`/admin/hosts/${hostId}/jobs`, adminKey, 'DELETE', undefined)
  },

  // A dedicated reboot job (for reboot_policy "prompt" / an explicit reboot).
  rebootHost(hostId: string, adminKey: string) {
    return adminWrite<Job>(`/admin/hosts/${hostId}/jobs`, adminKey, 'POST', {
      job_type: 'reboot',
      requested_by: 'dashboard',
    })
  },

  patchHost(
    hostId: string,
    adminKey: string,
    body: {
      reboot_policy?: RebootPolicy
      is_active?: boolean
      tags?: Record<string, string>
    },
  ) {
    return adminWrite<{
      id: string
      hostname: string
      reboot_policy: RebootPolicy
      is_active: boolean
      tags: Record<string, string>
    }>(`/admin/hosts/${hostId}`, adminKey, 'PATCH', body)
  },

  deleteHost(hostId: string, adminKey: string) {
    return adminWrite<null>(`/admin/hosts/${hostId}`, adminKey, 'DELETE', undefined)
  },

  getSchedules: (hostId: string) => getJSON<Schedule[]>(`/hosts/${hostId}/schedules`),

  createSchedule(hostId: string, adminKey: string, body: ScheduleInput) {
    return adminWrite<Schedule>(`/admin/hosts/${hostId}/schedules`, adminKey, 'POST', body)
  },

  updateSchedule(scheduleId: string, adminKey: string, body: Partial<ScheduleInput>) {
    return adminWrite<Schedule>(`/admin/schedules/${scheduleId}`, adminKey, 'PATCH', body)
  },

  deleteSchedule(scheduleId: string, adminKey: string) {
    return adminWrite<null>(`/admin/schedules/${scheduleId}`, adminKey, 'DELETE', undefined)
  },

  listWebhooks: () => getJSON<Webhook[]>('/webhooks'),

  createWebhook(adminKey: string, body: WebhookInput) {
    return adminWrite<WebhookCreated>('/admin/webhooks', adminKey, 'POST', body)
  },

  updateWebhook(webhookId: string, adminKey: string, body: Partial<WebhookInput>) {
    return adminWrite<Webhook>(`/admin/webhooks/${webhookId}`, adminKey, 'PATCH', body)
  },

  deleteWebhook(webhookId: string, adminKey: string) {
    return adminWrite<null>(`/admin/webhooks/${webhookId}`, adminKey, 'DELETE', undefined)
  },

  testWebhook(webhookId: string, adminKey: string) {
    return adminWrite<{ delivery_id: string }>(
      `/admin/webhooks/${webhookId}/test`,
      adminKey,
      'POST',
      {},
    )
  },
}
