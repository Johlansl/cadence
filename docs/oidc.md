# Admin SSO via OIDC

Cadence can authenticate dashboard operators through any OpenID Connect
provider (reference deployment: Authentik) **in addition to** the shared
`X-Admin-Key`, which keeps working exactly as before. This page is the
operator setup; the design rationale lives in
[decisions.md](decisions.md#authentication), the threat model in
[SECURITY.md](../SECURITY.md#admin-sso-oidc).

## How it works

1. The dashboard shows **Sign in with SSO**. The browser is redirected to
   the provider, the operator logs in there.
2. The provider redirects back to Cadence with a one-time code. The
   **backend** exchanges it (the browser never sees any secret), validates
   the ID token locally (issuer -- one trailing slash tolerated on either
   side, nothing else -- audience, expiry, nonce, signature against the
   provider JWKS), and sets a sealed session cookie (8 h, HttpOnly,
   Secure, SameSite=Lax).
3. Writes made in a session are audited under the verified subject (email
   when verified, else `preferred_username`, else `sub`); a self-asserted
   `X-Actor` header is ignored on a session.
4. **Sign out** ends the Cadence session only. The provider SSO session, if
   any, survives (re-login there is silent until it expires).

If the provider is down, sign-in fails closed and the shared key keeps
working -- there is no lock-out path through SSO.

## Provider setup (Authentik)

In the Authentik admin interface:

1. Create an application plus an **OAuth2/OIDC provider** for Cadence.
2. Register this redirect URI on the provider (exact match):
   `https://cadence.lan/api/v1/auth/oidc/callback`
   (replace the host with your `CADENCE_SITE_ADDRESS`).
3. Note the provider's **issuer** (per application slug, of the form
   `https://<authentik-host>/application/o/<slug>/` -- confirm the exact
   value against your instance), **client ID**, and generate a **client
   secret**.
4. Attach the default OpenID claim mappings (`openid`, `email`,
   `profile`) to the provider. Without them the ID token carries only the
   opaque `sub` and the audit actor is unreadable; with them the actor is
   the email when verified, else the username. (Authentik's default email
   mapping reports `email_verified: false`, so expect the username.)

## Cadence configuration

In `.env` (mode `0600`, secrets never logged):

```sh
CADENCE_OIDC_ENABLED=true
CADENCE_OIDC_ISSUER=https://<authentik-host>/application/o/cadence/
CADENCE_OIDC_CLIENT_ID=<client id>
CADENCE_OIDC_CLIENT_SECRET=<client secret>
CADENCE_OIDC_REDIRECT_URI=https://cadence.lan/api/v1/auth/oidc/callback
```

The redirect URI must be byte-identical here and at the provider, or the
code exchange is refused. The issuer can be copied from the discovery
document as-is (trailing slash either way). Optional tuning: `CADENCE_OIDC_SCOPES`
(default `openid email profile`), `CADENCE_OIDC_SESSION_TTL_SECONDS`
(default 28800 = 8 h), `CADENCE_OIDC_CLOCK_SKEW_SECONDS` (default 120).
Then redeploy (`scripts/deploy.sh`, which recreates the backend with the
new environment -- a plain container restart does not pick up `.env`
changes).

With `CADENCE_OIDC_ENABLED` unset or any of issuer/client
id/secret/redirect URI empty, the `/api/v1/auth/*` endpoints 404 and every
admin write keeps requiring the shared key, bit-for-bit as before.

## Operations

- **Break-glass:** the shared key always works, provider or no provider.
  Keep it generated strong and stored offline.
- **Revocation:** there is no per-session revoke. A leaked session dies with
  its 8 h TTL; to kill every session at once, rotate
  `CADENCE_TOKEN_ENCRYPTION_KEY` (this also kills every agent token -- same
  story as the token plane, plan the rollout accordingly).
- **Audit:** filter `audit_log` on `actor`. Rows saying `admin` (or a custom
  `X-Actor` value) predate SSO or came through the key; rows with an email
  or username came through a verified session. Historic rows are never
  rewritten.
- **Troubleshooting:** sign-in failures are 400 (bad state/provider
  refusal), 401 (bad token), or 502 (provider unreachable: DNS, refused,
  timeout, TLS, discovery/JWKS HTTP errors) without detail to the browser
  and without traceback in the log; the backend log carries the reason.
  Common causes: clock drift past the skew window, redirect URI mismatch,
  missing claim mappings (actor falls back to the opaque `sub`), or a
  provider the backend cannot reach (it must resolve the issuer over the
  network, independent of the browser path). Discovery and JWKS are cached
  for an hour: while the cache is warm `/login` still redirects to the
  provider and the sign-in then fails there -- no session is minted either
  way.

## Verified against the reference

Exercised end to end against Authentik `2026.8.2` (provider
`cadence-dashboard` on application `cadence`, explicit-consent
authorization flow, claim mappings `openid`/`email`/`profile` attached):
login redirect with the registered redirect URI, scripted
identification + password + consent to a real authorization code, backend
callback setting the session cookie, `/me` reporting the username actor
with a stable `sub`, a session-only admin write with a spoofed `X-Actor`
audited under the verified subject, logout, and the provider-down matrix
(session `/me` still 200 offline, `X-Admin-Key` writes still 201,
`/login` 502 with a cold cache). Two findings from that run are fixed in
the code, not just documented: the issuer trailing-slash tolerance above
(Authentik emits the slash) and the 502 mapping for transport failures
(a dead provider answered 500 before).
