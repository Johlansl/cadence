import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { pill, type Tone } from '../lib/pill'
import type { AgentCertificate, AgentCertificateState, HostSummary } from '../types'
import { AdminActionFeedback, useAdminKeyAction } from './AdminKeyPrompt'
import { useConfirm } from './ConfirmDialog'
import { RelativeTime } from './RelativeTime'
import { useToast } from './Toast'

const POLL_MS = 30_000

const field =
  'rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-200 outline-none focus:border-zinc-500'
const ghostBtn =
  'rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:text-zinc-200 disabled:opacity-50'

const STATE_TONE: Record<AgentCertificateState, Tone> = {
  active: 'ok',
  expired: 'neutral',
  revoked: 'danger',
}

export function EnrollmentCertificates({ hosts }: { hosts: HostSummary[] }) {
  const [hostId, setHostId] = useState('')
  const host = hosts.find((candidate) => candidate.id === hostId)

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-xs uppercase tracking-wide text-zinc-600">Client certificates</h3>
          <p className="mt-1 text-xs text-zinc-500">
            Inspect and revoke the mTLS identities issued during enrollment.
          </p>
        </div>
        <select
          value={hostId}
          onChange={(event) => setHostId(event.target.value)}
          aria-label="Certificate host"
          className={field}
        >
          <option value="">select a host…</option>
          {hosts.map((candidate) => (
            <option key={candidate.id} value={candidate.id}>
              {candidate.hostname}
              {candidate.is_active ? '' : ' (inactive)'}
            </option>
          ))}
        </select>
      </div>

      {host ? (
        <CertificateList key={host.id} host={host} />
      ) : (
        <p className="text-sm text-zinc-600">Select a host to inspect its certificates.</p>
      )}
    </section>
  )
}

function CertificateList({ host }: { host: HostSummary }) {
  const [certificates, setCertificates] = useState<AgentCertificate[] | null>(null)
  const loadAction = useCallback(
    (key: string) => api.listAgentCertificates(host.id, key),
    [host.id],
  )
  const loader = useAdminKeyAction(loadAction)
  const runLoad = loader.run

  const acceptCertificates = useCallback((data: unknown) => {
    if (Array.isArray(data)) setCertificates(data as AgentCertificate[])
  }, [])
  const refresh = useCallback(async () => {
    const result = await runLoad()
    if (result?.ok) acceptCertificates(result.data)
  }, [acceptCertificates, runLoad])

  useEffect(() => {
    void refresh()
    const timer = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(timer)
  }, [refresh])

  return (
    <div className="space-y-2">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={loader.busy}
          className={ghostBtn}
        >
          {loader.busy ? 'refreshing…' : 'refresh certificates'}
        </button>
      </div>

      <AdminActionFeedback
        actions={loader}
        onKeyAccepted={(result) => {
          if (result?.ok) acceptCertificates(result.data)
        }}
      />

      {certificates === null ? (
        <p className="text-sm text-zinc-600">Enter the admin key to load certificates.</p>
      ) : certificates.length === 0 ? (
        <p className="text-sm text-zinc-600">No client certificate has been issued to this host.</p>
      ) : (
        <ul className="space-y-2">
          {certificates.map((certificate) => (
            <li key={certificate.id}>
              <CertificateRow
                certificate={certificate}
                hostname={host.hostname}
                onChanged={refresh}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function CertificateRow({
  certificate,
  hostname,
  onChanged,
}: {
  certificate: AgentCertificate
  hostname: string
  onChanged: () => void
}) {
  const confirm = useConfirm()
  const toast = useToast()
  const revoker = useAdminKeyAction((key) =>
    api.revokeAgentCertificate(certificate.host_id, certificate.id, key),
  )

  const acceptRevoked = () => {
    toast.notify('success', 'Client certificate revoked.')
    onChanged()
  }

  const revoke = async () => {
    if (
      !(await confirm({
        title: `Revoke the client certificate for ${hostname}?`,
        body: 'The certificate will immediately lose access to the mTLS agent port. Confirm that the host has another usable certificate or is ready to be re-enrolled.',
        confirmLabel: 'Revoke certificate',
        danger: true,
      }))
    )
      return
    const result = await revoker.run()
    if (result?.ok) acceptRevoked()
  }

  return (
    <div className="rounded border border-zinc-800 bg-zinc-900/30 px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className={pill(STATE_TONE[certificate.state])}>{certificate.state}</span>
          <span className="font-mono text-zinc-500">serial {certificate.serial_number}</span>
        </div>
        {certificate.state === 'active' && (
          <button
            type="button"
            onClick={() => void revoke()}
            disabled={revoker.busy}
            className={ghostBtn}
          >
            {revoker.busy ? 'revoking…' : 'revoke certificate'}
          </button>
        )}
      </div>
      <dl className="mt-2 grid gap-x-3 gap-y-1 text-zinc-500 sm:grid-cols-[max-content_1fr]">
        <dt>SHA-256</dt>
        <dd className="break-all font-mono text-zinc-400">{certificate.fingerprint_sha256}</dd>
        <dt>issued</dt>
        <dd>
          <RelativeTime iso={certificate.issued_at} />
        </dd>
        <dt>expires</dt>
        <dd>
          <RelativeTime iso={certificate.expires_at} />
        </dd>
        <dt>last used</dt>
        <dd>
          {certificate.last_used_at ? <RelativeTime iso={certificate.last_used_at} /> : 'never'}
        </dd>
      </dl>
      <AdminActionFeedback
        actions={revoker}
        onKeyAccepted={(result) => {
          if (result?.ok) acceptRevoked()
        }}
      />
    </div>
  )
}
