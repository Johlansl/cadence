import { useCallback, useEffect, useState } from 'react'
import { api } from './api/client'
import { HostDetail } from './components/HostDetail'
import { HostList } from './components/HostList'
import { OverviewChips, SilentBanner } from './components/Overview'
import { relativeTime } from './lib/time'
import type { HostDetail as HostDetailData, HostSummary } from './types'

const POLL_MS = 30_000

export default function App() {
  const [hosts, setHosts] = useState<HostSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<HostDetailData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const [detailReload, setDetailReload] = useState(0)
  const [, tick] = useState(0)

  const refreshList = useCallback(async () => {
    try {
      setHosts(await api.listHosts())
      setError(null)
      setLastSync(new Date())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  // Poll the host list.
  useEffect(() => {
    void refreshList()
    const t = setInterval(() => void refreshList(), POLL_MS)
    return () => clearInterval(t)
  }, [refreshList])

  // Poll the open host's detail.
  useEffect(() => {
    if (!selectedId) {
      setDetail(null)
      return
    }
    let cancelled = false
    const load = () =>
      api
        .getHost(selectedId)
        .then((d) => {
          if (!cancelled) setDetail(d)
        })
        .catch((e) => {
          if (!cancelled) setError(e instanceof Error ? e.message : String(e))
        })
    void load()
    const t = setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [selectedId, detailReload])

  // Called after a host mutation from the detail pane.
  const onHostChanged = useCallback(() => {
    void refreshList()
    setDetailReload((n) => n + 1)
  }, [refreshList])
  const onHostDeleted = useCallback(() => {
    setSelectedId(null)
    void refreshList()
  }, [refreshList])

  // Select the first host once the list arrives.
  useEffect(() => {
    if (!selectedId && hosts.length > 0) setSelectedId(hosts[0].id)
  }, [hosts, selectedId])

  // Keep relative timestamps moving between polls.
  useEffect(() => {
    const t = setInterval(() => tick((n) => n + 1), 15_000)
    return () => clearInterval(t)
  }, [])

  return (
    <div className="flex h-full flex-col bg-zinc-950">
      <header className="flex items-center justify-between border-b border-zinc-800 px-6 py-3">
        <div className="flex items-baseline gap-3">
          <h1 className="text-sm font-semibold uppercase tracking-widest text-zinc-300">Cadence</h1>
          <OverviewChips hosts={hosts} />
        </div>
        <div className="text-xs text-zinc-600">
          {error ? (
            <span className="text-red-400">error: {error}</span>
          ) : lastSync ? (
            <span>synced {relativeTime(lastSync.toISOString())}</span>
          ) : (
            <span>loading…</span>
          )}
        </div>
      </header>

      <SilentBanner hosts={hosts} onSelect={setSelectedId} />

      <div className="flex min-h-0 flex-1">
        <aside className="w-80 shrink-0 overflow-auto border-r border-zinc-800">
          <HostList hosts={hosts} selectedId={selectedId} onSelect={setSelectedId} />
        </aside>
        <main className="min-w-0 flex-1 overflow-hidden">
          {detail ? (
            <HostDetail host={detail} onChanged={onHostChanged} onDeleted={onHostDeleted} />
          ) : (
            <p className="p-6 text-sm text-zinc-600">Select a host.</p>
          )}
        </main>
      </div>
    </div>
  )
}
