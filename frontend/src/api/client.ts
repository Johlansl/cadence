import type { HostDetail, HostSummary, Job } from '../types'

const BASE = '/api/v1'

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

export interface CreateJobResult {
  ok: boolean
  status: number
  job?: Job
  detail?: string
}

export const api = {
  listHosts: () => getJSON<HostSummary[]>('/hosts'),
  getHost: (id: string) => getJSON<HostDetail>(`/hosts/${id}`),
  getHostJobs: (id: string) => getJSON<Job[]>(`/hosts/${id}/jobs`),

  async createJob(hostId: string, adminKey: string): Promise<CreateJobResult> {
    const res = await fetch(`${BASE}/admin/hosts/${hostId}/jobs`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-Admin-Key': adminKey,
      },
      body: JSON.stringify({ requested_by: 'dashboard' }),
    })
    if (res.status === 201) {
      return { ok: true, status: 201, job: (await res.json()) as Job }
    }
    let detail: string | undefined
    try {
      detail = ((await res.json()) as { detail?: string }).detail
    } catch {
      /* no JSON body */
    }
    return { ok: false, status: res.status, detail }
  },
}
