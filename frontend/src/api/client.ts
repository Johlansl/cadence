import type { HostDetail, HostSummary } from '../types'

const BASE = '/api/v1'

async function getJSON<T>(path: string): Promise<T> {
  const res = await fetch(BASE + path, { headers: { Accept: 'application/json' } })
  if (!res.ok) {
    throw new Error(`${res.status} ${res.statusText}`)
  }
  return (await res.json()) as T
}

export const api = {
  listHosts: () => getJSON<HostSummary[]>('/hosts'),
  getHost: (id: string) => getJSON<HostDetail>(`/hosts/${id}`),
}
