"""Admin SSO via OIDC (roadmap item 10).

Complements the shared X-Admin-Key, it does not replace it. No new
dependencies: JWT parsing is base64url + JSON (stdlib) and signatures verify
with `cryptography` (already required). Sessions are Fernet-sealed cookies,
stateless, with no DB table.
"""