import type {
  HostDetail,
  HostSummary,
  Job,
  RebootPolicy,
  Schedule,
  ScheduleInput,
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
    detail = ((await res.json()) as { detail?: string }).detail
  } catch {
    /* no JSON body */
  }
  return { ok: false, status: res.status, detail }
}

export const api = {
  listHosts: () => getJSON<HostSummary[]>('/hosts'),
  getHost: (id: string) => getJSON<HostDetail>(`/hosts/${id}`),
  getHostJobs: (id: string) => getJSON<Job[]>(`/hosts/${id}/jobs`),

  // reboot: omit to use the host's reboot_policy; set to override for this job.
  createJob(hostId: string, adminKey: string, reboot?: RebootPolicy) {
    const body: Record<string, unknown> = { requested_by: 'dashboard' }
    if (reboot) body.params = { reboot }
    return adminWrite<Job>(`/admin/hosts/${hostId}/jobs`, adminKey, 'POST', body)
  },

  clearHostJobs(hostId: string, adminKey: string) {
    return adminWrite<null>(`/admin/hosts/${hostId}/jobs`, adminKey, 'DELETE', undefined)
  },

  patchHost(
    hostId: string,
    adminKey: string,
    body: { reboot_policy?: RebootPolicy; is_active?: boolean },
  ) {
    return adminWrite<{
      id: string
      hostname: string
      reboot_policy: RebootPolicy
      is_active: boolean
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
}
