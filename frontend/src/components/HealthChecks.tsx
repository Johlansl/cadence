import { pill, type Tone } from '../lib/pill'
import type { HealthCheck, HealthCheckDetails, HealthCheckPhase, HealthCheckStatus } from '../types'

const STATUS_TONE: Record<HealthCheckStatus, Tone> = {
  passed: 'ok',
  warning: 'warn',
  failed: 'danger',
  unknown: 'neutral',
  skipped: 'neutral',
}

const CHECK_LABELS: Record<HealthCheck['name'], string> = {
  disk_space: 'Disk space',
  package_manager_locks: 'Package manager locks',
  dpkg_audit: 'dpkg audit',
  apt_dependencies: 'APT dependencies',
  package_indexes: 'Package indexes',
  failed_services: 'Failed services',
  reboot_required: 'Reboot required',
}

function bytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let amount = value
  let unit = -1
  do {
    amount /= 1024
    unit++
  } while (amount >= 1024 && unit < units.length - 1)
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`
}

function Evidence({ details }: { details: HealthCheckDetails }) {
  const filesystems = details.filesystems ?? []
  const locks = details.locks ?? []
  const problems = details.problems ?? []
  const services = details.services ?? []
  const newServices = details.new_services ?? []
  const existingServices = details.existing_services ?? []
  const hasEvidence =
    filesystems.length > 0 ||
    locks.length > 0 ||
    problems.length > 0 ||
    services.length > 0 ||
    newServices.length > 0 ||
    existingServices.length > 0 ||
    details.strict_mode != null ||
    details.required != null

  if (!hasEvidence) return null

  return (
    <ul className="mt-1 space-y-0.5 font-mono text-[11px] text-zinc-500">
      {filesystems.map((fs) => (
        <li key={fs.paths.join('|')}>
          {fs.paths.join(', ')}: {bytes(fs.available_bytes)} available,{' '}
          {bytes(fs.minimum_available_bytes)} required
        </li>
      ))}
      {locks.map((lock) => (
        <li key={lock.path}>
          {lock.path}
          {lock.pid != null ? ` (PID ${lock.pid})` : ''}
        </li>
      ))}
      {problems.map((problem) => (
        <li key={problem}>{problem}</li>
      ))}
      {services.length > 0 && <li>failed: {services.join(', ')}</li>}
      {newServices.length > 0 && <li>newly failed: {newServices.join(', ')}</li>}
      {existingServices.length > 0 && <li>already failed: {existingServices.join(', ')}</li>}
      {details.strict_mode != null && (
        <li>strict repository checks: {details.strict_mode ? 'enabled' : 'disabled'}</li>
      )}
      {details.required != null && <li>reboot required: {details.required ? 'yes' : 'no'}</li>}
    </ul>
  )
}

function CheckRow({ check }: { check: HealthCheck }) {
  return (
    <li className="border-t border-zinc-800/80 py-2 first:border-t-0">
      <div className="flex flex-wrap items-start gap-2">
        <span className={pill(STATUS_TONE[check.status])}>{check.status}</span>
        <div className="min-w-0 flex-1">
          <div className="text-zinc-300">{CHECK_LABELS[check.name]}</div>
          <p className="text-zinc-500">{check.summary}</p>
          <Evidence details={check.details} />
        </div>
      </div>
    </li>
  )
}

function Phase({ label, phase }: { label: string; phase: HealthCheckPhase }) {
  return (
    <section>
      <div className="flex items-center gap-2">
        <h4 className="uppercase tracking-wide text-zinc-500">{label}</h4>
        <span className={pill(STATUS_TONE[phase.status])}>{phase.status}</span>
      </div>
      <ul className="mt-1">
        {phase.checks.map((check) => (
          <CheckRow key={check.name} check={check} />
        ))}
      </ul>
    </section>
  )
}

export function HealthChecks({
  pre,
  post,
}: {
  pre: HealthCheckPhase
  post?: HealthCheckPhase | null
}) {
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-zinc-500 hover:text-zinc-300">health checks</summary>
      <div className="mt-2 grid gap-3 rounded bg-zinc-950/70 p-3 lg:grid-cols-2">
        <Phase label="Before upgrade" phase={pre} />
        {post ? (
          <Phase label="After upgrade" phase={post} />
        ) : (
          <section>
            <h4 className="uppercase tracking-wide text-zinc-500">After upgrade</h4>
            <p className="mt-1 text-zinc-600">Not run because a pre-check blocked the upgrade.</p>
          </section>
        )}
      </div>
    </details>
  )
}
