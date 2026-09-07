# Contributing to Cadence

Thanks for your interest. Cadence is small and opinionated; a quick issue to
discuss an approach before a large PR usually saves everyone time.

## Ground rules

- **Agent code is standard-library only.** No new Go modules.
- **No new external dependency** (backend or frontend) without raising it first.
- **One focused, testable change per pull request.** Keep the relevant test
  suite green.
- **Schema changes go through a hand-written Alembic revision** under
  `backend/alembic/versions/` (no autogenerate). Describe the change and why in
  the PR.
- **Code and comments in English.**
- Some capabilities are intentionally out of scope for now, see
  [docs/decisions.md](docs/decisions.md#v1-scope). Please open an issue before
  building one of those.

See [docs/architecture.md](docs/architecture.md) for the layout and how the
pieces fit, and [README.md](README.md) for deployment/ops.

## Development

```sh
# agent
docker run --rm -v "$PWD/agent":/s -w /s golang:1.23 \
  sh -c 'go vet ./... && go test ./... && CGO_ENABLED=0 go build -o /dev/null ./cmd/agent'

# backend (needs the db service; runs pytest against a real PostgreSQL)
docker compose up -d db
docker compose run --rm -v "$PWD/backend:/app" backend \
  sh -c 'pip install -q -r requirements-dev.txt && pytest -q'

# frontend
docker run --rm -v "$PWD/frontend":/app -w /app node:22-alpine \
  sh -c 'npm ci && npm run lint && npm test && npm run build'

# whole stack from clean
docker compose up -d --build
```

CI runs the same three suites plus a full `docker compose` bring-up on every
push.

## Commits and sign-off

Write imperative commit subjects (`agent: retry apt-get on a held lock`) with a
body explaining the *why*.

Sign off your commits to certify the [Developer Certificate of
Origin](https://developercertificate.org/):

```sh
git commit -s
```

By contributing you agree that your contributions are licensed under the
project's [Apache-2.0](LICENSE) license.

## Reporting security issues

Do **not** open a public issue. See [SECURITY.md](SECURITY.md).
