import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from './api/client'
import { BulkActionBar } from './components/BulkActionBar'
import { CampaignsView } from './components/CampaignsView'
import { ErrorBoundary } from './components/ErrorBoundary'
import { EnrollmentView } from './components/EnrollmentView'
import { ExclusionsView } from './components/ExclusionsView'
import { FleetOverview } from './components/FleetOverview'
import { HostDetail } from './components/HostDetail'
import { HostFilters } from './components/HostFilters'
import { HostList } from './components/HostList'
import { OverviewChips, SilentBanner } from './components/Overview'
import { PackagesView } from './components/PackagesView'
import { RelativeTime } from './components/RelativeTime'
import { SessionButton, SessionProvider } from './components/SessionAuth'
import { WebhooksView } from './components/WebhooksView'
import { EMPTY_FILTERS, filterHosts, type HostFilters as Filters } from './lib/hostFilter'
import type { HostDetail as HostDetailData, HostSummary } from './types'

const POLL_MS = 30_000

// The current view is mirrored in the URL hash (#host=<id>, #packages,
// #webhooks, #exclusions, #campaigns, #enrollment) so a reload keeps the view
// and the link is shareable.
function readHashHostId(): string | null {
  const m = /(?:^|[#&])host=([^&]+)/.exec(window.location.hash)
  return m ? decodeURIComponent(m[1]) : null
}
function readHashIsPackages(): boolean {
  return window.location.hash === '#packages'
}
function readHashIsWebhooks(): boolean {
  return window.location.hash === '#webhooks'
}
function readHashIsExclusions(): boolean {
  return window.location.hash === '#exclusions'
}
function readHashIsCampaigns(): boolean {
  return window.location.hash === '#campaigns'
}
function readHashIsEnrollment(): boolean {
  return window.location.hash === '#enrollment'
}
function writeHash(next: string): void {
  if (window.location.hash === next) return
  const url = next || window.location.pathname + window.location.search
  window.history.replaceState(null, '', url)
}

export default function App() {
  const [hosts, setHosts] = useState<HostSummary[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(readHashHostId)
  const [showPackages, setShowPackages] = useState<boolean>(readHashIsPackages)
  const [showWebhooks, setShowWebhooks] = useState<boolean>(readHashIsWebhooks)
  const [showExclusions, setShowExclusions] = useState<boolean>(readHashIsExclusions)
  const [showCampaigns, setShowCampaigns] = useState<boolean>(readHashIsCampaigns)
  const [showEnrollment, setShowEnrollment] = useState<boolean>(readHashIsEnrollment)
  const [detail, setDetail] = useState<HostDetailData | null>(null)
  // Two independent failures: the host-list poll drives the global sync
  // indicator; a host-detail poll failure is shown in the detail pane only,
  // so a transient 500 on one host never blanks the whole header.
  const [listError, setListError] = useState<string | null>(null)
  const [listLoaded, setListLoaded] = useState(false)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [lastSync, setLastSync] = useState<Date | null>(null)
  const [detailReload, setDetailReload] = useState(0)
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS)
  const [checked, setChecked] = useState<Set<string>>(new Set())

  const visibleHosts = useMemo(() => filterHosts(hosts, filters), [hosts, filters])
  // Selected hosts hidden by the current filters: the selection persists, so
  // the bar must say how many of its targets are out of sight.
  const visibleIds = useMemo(() => new Set(visibleHosts.map((h) => h.id)), [visibleHosts])

  const toggleChecked = useCallback((id: string) => {
    setChecked((s) => {
      const next = new Set(s)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])
  const clearChecked = useCallback(() => setChecked(new Set()), [])

  const refreshList = useCallback(async () => {
    try {
      setHosts(await api.listHosts())
      setListError(null)
      setListLoaded(true)
      setLastSync(new Date())
    } catch (e) {
      setListError(e instanceof Error ? e.message : String(e))
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
      setDetailError(null)
      return
    }
    let cancelled = false
    setDetailError(null)
    const load = () =>
      api
        .getHost(selectedId)
        .then((d) => {
          if (!cancelled) {
            setDetail(d)
            setDetailError(null)
          }
        })
        .catch((e) => {
          if (!cancelled) setDetailError(e instanceof Error ? e.message : String(e))
        })
    void load()
    const t = setInterval(() => void load(), POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [selectedId, detailReload])

  const select = useCallback((id: string | null) => {
    setSelectedId(id)
    setShowPackages(false)
    setShowWebhooks(false)
    setShowExclusions(false)
    setShowCampaigns(false)
    setShowEnrollment(false)
    writeHash(id ? `#host=${encodeURIComponent(id)}` : '')
  }, [])

  const selectPackages = useCallback(() => {
    setSelectedId(null)
    setShowPackages(true)
    setShowWebhooks(false)
    setShowExclusions(false)
    setShowCampaigns(false)
    setShowEnrollment(false)
    writeHash('#packages')
  }, [])

  const selectWebhooks = useCallback(() => {
    setSelectedId(null)
    setShowPackages(false)
    setShowWebhooks(true)
    setShowExclusions(false)
    setShowCampaigns(false)
    setShowEnrollment(false)
    writeHash('#webhooks')
  }, [])

  const selectExclusions = useCallback(() => {
    setSelectedId(null)
    setShowPackages(false)
    setShowWebhooks(false)
    setShowExclusions(true)
    setShowCampaigns(false)
    setShowEnrollment(false)
    writeHash('#exclusions')
  }, [])

  const selectCampaigns = useCallback(() => {
    setSelectedId(null)
    setShowPackages(false)
    setShowWebhooks(false)
    setShowExclusions(false)
    setShowCampaigns(true)
    setShowEnrollment(false)
    writeHash('#campaigns')
  }, [])

  const selectEnrollment = useCallback(() => {
    setSelectedId(null)
    setShowPackages(false)
    setShowWebhooks(false)
    setShowExclusions(false)
    setShowCampaigns(false)
    setShowEnrollment(true)
    writeHash('#enrollment')
  }, [])

  // Called after a host mutation from the detail pane.
  const onHostChanged = useCallback(() => {
    void refreshList()
    setDetailReload((n) => n + 1)
  }, [refreshList])
  const onHostDeleted = useCallback(() => {
    select(null)
    void refreshList()
  }, [refreshList, select])

  // Follow back/forward navigation between hosts and top-level views.
  useEffect(() => {
    const onHashChange = () => {
      setSelectedId(readHashHostId())
      setShowPackages(readHashIsPackages())
      setShowWebhooks(readHashIsWebhooks())
      setShowExclusions(readHashIsExclusions())
      setShowCampaigns(readHashIsCampaigns())
      setShowEnrollment(readHashIsEnrollment())
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return (
    <SessionProvider>
      <div className="flex h-full flex-col bg-zinc-950">
        <header className="flex items-center justify-between border-b border-zinc-800 px-6 py-3">
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => select(null)}
              className="rounded px-1 py-1 text-sm font-semibold uppercase tracking-widest text-zinc-300 hover:text-zinc-100"
              title="Back to the fleet overview"
            >
              Cadence
            </button>
            <nav aria-label="Primary" className="flex items-center gap-1">
              <button
                type="button"
                onClick={selectPackages}
                aria-current={showPackages ? 'page' : undefined}
                className={`rounded px-2 py-1 text-xs uppercase tracking-widest ${
                  showPackages
                    ? 'bg-zinc-800/70 text-zinc-100'
                    : 'text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300'
                }`}
              >
                Packages
              </button>
              <button
                type="button"
                onClick={selectWebhooks}
                aria-current={showWebhooks ? 'page' : undefined}
                className={`rounded px-2 py-1 text-xs uppercase tracking-widest ${
                  showWebhooks
                    ? 'bg-zinc-800/70 text-zinc-100'
                    : 'text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300'
                }`}
              >
                Webhooks
              </button>
              <button
                type="button"
                onClick={selectExclusions}
                aria-current={showExclusions ? 'page' : undefined}
                className={`rounded px-2 py-1 text-xs uppercase tracking-widest ${
                  showExclusions
                    ? 'bg-zinc-800/70 text-zinc-100'
                    : 'text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300'
                }`}
              >
                Exclusions
              </button>
              <button
                type="button"
                onClick={selectCampaigns}
                aria-current={showCampaigns ? 'page' : undefined}
                className={`rounded px-2 py-1 text-xs uppercase tracking-widest ${
                  showCampaigns
                    ? 'bg-zinc-800/70 text-zinc-100'
                    : 'text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300'
                }`}
              >
                Campaigns
              </button>
              <button
                type="button"
                onClick={selectEnrollment}
                aria-current={showEnrollment ? 'page' : undefined}
                className={`rounded px-2 py-1 text-xs uppercase tracking-widest ${
                  showEnrollment
                    ? 'bg-zinc-800/70 text-zinc-100'
                    : 'text-zinc-500 hover:bg-zinc-800/40 hover:text-zinc-300'
                }`}
              >
                Enrollment
              </button>
            </nav>
            {listLoaded && (
              <div className="border-l border-zinc-800 pl-3">
                <OverviewChips hosts={hosts} />
              </div>
            )}
          </div>
          <div className="flex items-center gap-3 text-xs text-zinc-600">
            <SessionButton />
            {listError ? (
              <span className="text-red-400">sync error: {listError}</span>
            ) : lastSync ? (
              <span>
                synced <RelativeTime iso={lastSync.toISOString()} />
              </span>
            ) : (
              <span>loading…</span>
            )}
          </div>
        </header>

        <SilentBanner
          hosts={hosts}
          onShowSilent={() => {
            select(null)
            setFilters((f) => ({ ...f, freshness: 'silent' }))
          }}
        />

        <div className="flex min-h-0 flex-1">
          <aside className="flex w-80 shrink-0 flex-col overflow-hidden border-r border-zinc-800">
            <HostFilters
              value={filters}
              onChange={setFilters}
              shown={visibleHosts.length}
              total={hosts.length}
            />
            {checked.size > 0 && (
              <BulkActionBar
                hostIds={[...checked]}
                hiddenCount={[...checked].filter((id) => !visibleIds.has(id)).length}
                onClear={clearChecked}
                onDone={() => {
                  clearChecked()
                  void refreshList()
                }}
              />
            )}
            <nav aria-label="Hosts" className="min-h-0 flex-1 overflow-auto">
              <HostList
                hosts={visibleHosts}
                selectedId={selectedId}
                attentionOrder={filters.attention}
                onSelect={select}
                emptyLabel={
                  hosts.length === 0 ? 'No hosts registered.' : 'No hosts match the filter.'
                }
                checkedIds={checked}
                onToggleCheck={toggleChecked}
              />
            </nav>
          </aside>
          <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
            {showEnrollment ? (
              <EnrollmentView hosts={hosts} />
            ) : showCampaigns ? (
              <CampaignsView onSelectHost={select} />
            ) : showWebhooks ? (
              <WebhooksView />
            ) : showExclusions ? (
              <ExclusionsView />
            ) : showPackages ? (
              <PackagesView onSelectHost={select} />
            ) : detail ? (
              <>
                {detailError && (
                  <p className="shrink-0 border-b border-amber-500/30 bg-amber-500/10 px-6 py-1.5 text-xs text-amber-400">
                    couldn't refresh this host: {detailError}
                  </p>
                )}
                <div className="min-h-0 flex-1">
                  <ErrorBoundary key={detail.id}>
                    <HostDetail host={detail} onChanged={onHostChanged} onDeleted={onHostDeleted} />
                  </ErrorBoundary>
                </div>
              </>
            ) : selectedId ? (
              <p className="p-6 text-sm text-red-400">
                {detailError ? `failed to load host: ${detailError}` : 'loading…'}
              </p>
            ) : !listLoaded && listError ? (
              <p className="p-6 text-sm text-red-400">failed to load hosts: {listError}</p>
            ) : !listLoaded ? (
              <p className="p-6 text-sm text-zinc-500">loading…</p>
            ) : (
              <FleetOverview
                hosts={hosts}
                onSelect={select}
                onShowAttention={() => setFilters((f) => ({ ...f, attention: true }))}
              />
            )}
          </main>
        </div>
      </div>
    </SessionProvider>
  )
}
