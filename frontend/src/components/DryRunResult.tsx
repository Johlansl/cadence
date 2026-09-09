import { pill } from '../lib/pill'
import type { DryRunPkg, DryRunResult as DryRunResultData } from '../types'

function PkgRows({ pkgs }: { pkgs: DryRunPkg[] }) {
  return (
    <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-zinc-400">
      {pkgs.map((p) => (
        <li key={`${p.name}/${p.architecture ?? ''}`}>
          {p.name}
          {p.installed_version && p.candidate_version ? (
            <span className="text-zinc-500">
              {' '}
              {p.installed_version} <span aria-hidden>-&gt;</span> {p.candidate_version}
            </span>
          ) : (
            (p.installed_version || p.candidate_version) && (
              <span className="text-zinc-500"> {p.installed_version || p.candidate_version}</span>
            )
          )}
          {p.is_security_update && <span className={`ml-1 ${pill('warn')}`}>sec</span>}
        </li>
      ))}
    </ul>
  )
}

function NameList({ names }: { names: string[] }) {
  return (
    <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-zinc-400">
      {names.map((n) => (
        <li key={n}>{n}</li>
      ))}
    </ul>
  )
}

// Compact render of an apt_dry_run job's structured preview (roadmap item 4):
// a one-line count strip plus a collapsible list per non-empty category. The
// raw simulation log stays available under the generic "log" details in Jobs.
export function DryRunResult({ data }: { data: DryRunResultData }) {
  const updated = data.updated ?? []
  const newly = data.newly_installed ?? []
  const removed = data.removed ?? []
  const keptBack = data.kept_back ?? []
  const excluded = data.excluded ?? []
  const heldInPlace = data.held_in_place ?? []

  const segments = [
    updated.length ? `${updated.length} to upgrade` : '',
    newly.length ? `${newly.length} new` : '',
    removed.length ? `${removed.length} to remove` : '',
    keptBack.length ? `${keptBack.length} kept back` : '',
    excluded.length ? `${excluded.length} excluded by policy` : '',
  ].filter(Boolean)

  return (
    <div className="mt-1">
      <p className="text-[11px] text-zinc-500">
        {segments.length ? segments.join(' · ') : 'nothing to do: the host is up to date'}
      </p>

      {updated.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            would upgrade ({updated.length})
          </summary>
          <PkgRows pkgs={updated} />
        </details>
      )}
      {newly.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            would newly install ({newly.length})
          </summary>
          <PkgRows pkgs={newly} />
        </details>
      )}
      {removed.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-red-400/80 hover:text-red-300">
            would remove ({removed.length})
          </summary>
          <PkgRows pkgs={removed} />
        </details>
      )}
      {keptBack.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            kept back by apt ({keptBack.length})
          </summary>
          <NameList names={keptBack} />
        </details>
      )}
      {excluded.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">
            excluded by policy ({excluded.length})
          </summary>
          <NameList names={excluded} />
        </details>
      )}
      {heldInPlace.length > 0 && (
        <p className="mt-1 text-[11px] text-amber-400/80">
          already on hold on the host: {heldInPlace.join(', ')}
        </p>
      )}
    </div>
  )
}
