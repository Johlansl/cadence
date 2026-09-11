# Agent enrollment and mTLS migration

Cadence uses one hostname with two TLS listeners:

- `https://cadence.lan:443` serves the dashboard, admin API, enrollment claim
  and authenticated installation assets;
- `https://cadence.lan:8443` serves post-enrollment agent endpoints and
  requires a client certificate.

The agent still signs every application request with its per-host HMAC token.
Caddy's verified client-certificate fingerprint is passed through a
proxy-authenticated header, and the backend requires the certificate and HMAC
token to resolve to the same host UUID.

## First-contact trust

The enrollment code has the form
`cad1.<192-bit-secret>.<server-CA-SHA256>`. It is stored server-side only as a
secret hash, expires after 30 minutes by default (configurable from 5 to 240),
and is consumed atomically after credentials are issued.

Copy `scripts/agent-bootstrap.sh` to the target through an authenticated
channel. It is stable trusted code and must never be downloaded over HTTP. The
prelude retrieves only `/agent/ca.crt` over HTTP and compares its exact bytes
with the fingerprint in the manually entered code. It obtains the installer
and all remaining assets over HTTPS only after that check succeeds.

The host creates its ECDSA P-256 private key locally. The backend signs its CSR
with the private client PKI and returns a 90-day `clientAuth` certificate. A
regular agent run renews at 14 days before expiry. Renewal writes a versioned
key/certificate pair and switches `agent.env` last; the old certificate is not
immediately revoked, so a lost response cannot lock the host out.

## Server preflight

Before any deployment, add the new settings to `.env`:

```sh
CADENCE_AGENT_PORT=8443
CADENCE_LEGACY_AGENT_ENDPOINTS=on
CADENCE_INTERNAL_PROXY_KEY=<openssl-rand-hex-32-output>
```

`CADENCE_LEGACY_AGENT_ENDPOINTS` has no implicit Compose value. The deployment
fails before changing containers unless it is explicitly `on` or `off`. Keep
it `on` throughout the migration of the existing fleet. Open TCP 8443 from
managed hosts to the Cadence server; no second DNS or `/etc/hosts` entry is
needed.

Back up before deployment. `scripts/backup.sh` now includes both
`caddy_data.tgz` (server TLS CA) and `client_pki.tgz` (client root/intermediate
and their private keys). `scripts/restore-check.sh` validates both in isolated
volumes.

## Host-by-host migration

Do not delete or revoke a host's existing token during migration. For one host:

1. Find its existing host UUID and create a migration-bound code:

   ```sh
   scripts/provision-host.sh --host-id <host-uuid> <hostname>
   ```

2. Securely copy `scripts/agent-bootstrap.sh` to that host and run it as root:

   ```sh
   sudo CADENCE_DASHBOARD_URL=https://cadence.lan ./agent-bootstrap.sh
   ```

3. Enter the code at the prompt. Confirm `/etc/cadence/agent.env` now points to
   `https://cadence.lan:8443` and contains all three certificate/CA paths.
4. Confirm a full report, several one-minute polls, the boot/manual health-check
   path and an `apt_dry_run` complete. The admin certificate list must show a
   fresh `last_used_at`; the token list must show the new enrollment token in
   use. The old token remains a rollback credential until validation ends.
5. Confirm a request to 8443 without a client certificate fails at TLS, and a
   valid certificate paired with another host's HMAC token is rejected.

For the current fleet, migrate `vm-japp` first. Keep `vm-nginxproxy` on the
legacy 443 path while observing `vm-japp` for at least 30 minutes, one complete
report cycle and multiple poll cycles. This is the exact end of
sub-delivery A. EnrollmentView work must not begin until this real-host gate is
complete and separately approved.

After the frontend sub-delivery is itself approved and deployed, migrate
`vm-nginxproxy` with the same procedure. Only when both hosts have demonstrated
mTLS traffic may a separate approved deployment change:

```sh
CADENCE_LEGACY_AGENT_ENDPOINTS=off
```

That switch makes all legacy agent routes on 443 return 404. It does not alter
the HMAC verifier or revoke stored legacy tokens.

For later binary-only upgrades, download the installer over HTTPS with the
already pinned `/usr/local/share/ca-certificates/cadence-server.crt`, save it
to a local file, and run it with `CADENCE_UPGRADE_ONLY=true`. This mode refuses
to run unless an enrolled `agent.env` already exists and never replaces its
token or certificate paths.

## Recovery and revocation

List or revoke certificates with the admin API:

```sh
curl -s https://cadence.lan/api/v1/admin/hosts/<host-id>/certificates \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
curl -s -X DELETE \
  https://cadence.lan/api/v1/admin/hosts/<host-id>/certificates/<certificate-id> \
  -H "X-Admin-Key: $CADENCE_ADMIN_KEY"
```

If a host loses its key or its only certificate expires, issue a new
host-bound enrollment code and repeat the bootstrap. Revoking a certificate
does not revoke HMAC tokens, and revoking a token does not revoke certificates;
both layers must authenticate for agent traffic to succeed.
