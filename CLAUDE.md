# Working in this repo (Claude Code)

Cadence is a lightweight patch-management tool for Linux servers: a stdlib-only
Go agent reports installed packages and available apt updates, a FastAPI +
PostgreSQL backend stores fleet state and queues actions, and a React dashboard
shows status and triggers upgrades or reboots per host. It runs as a
`docker compose` stack (`db`, `backend`, `scheduler`, `frontend`, `caddy`).

- **Architecture:** [docs/architecture.md](docs/architecture.md)
- **Why it's built this way, and V1 scope:** [docs/decisions.md](docs/decisions.md)
- **Threat model:** [SECURITY.md](SECURITY.md)
- **Ops / deploy / test runbooks:** [README.md](README.md)

## Rules

- **Agent: standard library only.** No external Go modules — config via
  environment variables, one static `CGO_ENABLED=0` binary.
- **One testable increment per commit.** No large commit mixing several changes;
  each commit leaves the relevant test suite green.
- **Schema changes go through hand-written Alembic revisions** (no
  autogenerate). Propose the change and the reason before applying it.
- **Code and comments in English.**
- **No new external dependency without calling it out first.**
- Target host is small (≈2 vCPU / 2 GB, `docker compose`). Don't assume extra
  tooling (Kubernetes, a private registry, …).
- The items listed as out-of-scope in
  [docs/decisions.md](docs/decisions.md#v1-scope) (package exclusions, reboot
  sequencing, notifications, multi-distro, RBAC) are deliberate. Don't
  implement them mid-task — leave a TODO and ask.
- **No `Co-Authored-By` trailer on commits in this repo.** Every commit here
  is mirrored to the public GitHub repo (see
  [MAINTAINING.md](MAINTAINING.md)), which is meant to show zero AI-assistance
  trace — no trailer, no mention of Claude/Anthropic anywhere. A leaked
  trailer put an AI assistant in the public repo's Contributors list on
  2026-09-05; fixed with a rebase + one-off `push --force` on the mirror only
  (see MAINTAINING.md). Applies here in `~/cadence` too, not just at mirror
  time — the source commit must never carry it.
- **Non-trivial change → plan first, get the maintainer's sign-off, then
  code.** Small obvious fixes don't need it.
- **Every `git push` (GitLab or GitHub) and every Alembic migration is shown
  and confirmed with the maintainer before running.** Never force-push except
  a maintainer-approved, documented one-off. The GitLab push is done via a
  stored PAT (redact it in any shown output); the GitHub mirror push and tags
  use the SSH key. A published `agent-v*` tag is never moved.
- **`vm-cadence` and its Proxmox host are monitor-only — never auto-patched by
  Cadence.** `reboot_policy` stays `never`, no schedule targets them, their
  upgrades are operator-run out of band. See
  [docs/decisions.md](docs/decisions.md#monitoring-the-control-plane).
- **Open-source posture (2026-09-06):** build for an unknown external deployer,
  now — see [docs/decisions.md](docs/decisions.md#open-source-posture). Two
  earlier policies sit *under* that framing and are **candidates to re-examine
  when the maintainer chooses** (do not change them unprompted): (1) zero
  AI-assistance trace in the public mirror; (2) the private GitLab repo as the
  one source of truth, one-way replay to GitHub per MAINTAINING.md. Both remain
  in force.

## Layout

```
agent/      Go agent: cmd/agent, internal/{config,collector,client,report,executor,rebootcheck,reboot,apterr,logging}, systemd/
backend/    FastAPI app (app/), Alembic migrations (alembic/), tests (tests/)
frontend/   React + Vite + Tailwind (src/)
scripts/    deploy / backup(+signing-key) / restore-check(+restore-signing-key) / provision-host / publish-agent / gen-secrets / rotate-dashboard-password / agent-install
docs/       architecture, decisions
docker-compose.yml   Caddyfile
```

## Build & test

```sh
# agent
docker run --rm -v "$PWD/agent":/s -w /s golang:1.23 \
  sh -c 'go vet ./... && go test ./... && CGO_ENABLED=0 go build -o /dev/null ./cmd/agent'

# backend (needs the db service up; runs pytest against a real PostgreSQL)
docker compose run --rm -v "$PWD/backend:/app" backend \
  sh -c 'pip install -q -r requirements-dev.txt && pytest -q'

# frontend
docker run --rm -v "$PWD/frontend":/app -w /app node:22-alpine \
  sh -c 'npm ci && npm run lint && npm test && npm run build'

# whole stack, from clean
docker compose up -d --build         # first run must build; Alembic creates the schema on boot
docker compose run --rm backend alembic current   # -> head

# redeploy on the server (rebuild + migrate + restage the served agent binary)
scripts/deploy.sh
```
