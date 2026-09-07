# Maintaining Cadence — private ↔ public mirror

Cadence is developed in a **private GitLab repo** and mirrored to a **public
GitHub repo**. This file documents that workflow. It is intentionally **kept out
of the public mirror** (alongside `.gitlab-ci.yml` and `CLAUDE.md`) because it
only concerns this project's dual-repo setup.

## The two repos

| Repo | Path on `vm-cadence` | Remote | Role |
|------|----------------------|--------|------|
| Private | `~/cadence` | `https://gitlab.com/Johlansl/cadence.git` (project `85865420`) | Working repo. Every change lands here first. Fast-forward only on `main`. |
| Public | `~/cadence-public` | `git@github.com:Johlansl/cadence.git` | Mirror. Never edited directly — only re-seeded from the private tree. |

### Intended divergence

The public tree is byte-for-byte the private tree **except** these files, which
live only in the private repo:

- `.gitlab-ci.yml` — the public repo uses `.github/workflows/ci.yml` instead
- `CLAUDE.md` — Claude Code working notes
- `MAINTAINING.md` — this file

Anything else that differs is a mistake.

## Routine change (private repo)

```sh
cd ~/cadence
git switch -c <branch>                 # don't commit straight to main
# ... edit, test (build/test matrix in README.md / CLAUDE.md) ...
git commit
git switch main && git merge --ff-only <branch>
```

### Pushing to GitLab

The stored PAT is short-lived and is usually already revoked. Mint a fresh one
(GitLab → Settings → Access Tokens, `write_repository` scope) and pass it inline
— do **not** persist it in the remote URL or `.git/config`:

```sh
git push https://oauth2:<PAT>@gitlab.com/Johlansl/cadence.git main:main
```

Poll the pipeline:

```sh
curl --header "PRIVATE-TOKEN: <PAT>" \
  "https://gitlab.com/api/v4/projects/85865420/pipelines?ref=main&per_page=1"
```

## Keeping the two CI configs in sync

`.gitlab-ci.yml` (private) and `.github/workflows/ci.yml` (public) must run the
**same five jobs**: `agent`, `scripts`, `backend`, `frontend`, `stack`. When the
test matrix changes, update both in the same change.

## Syncing new commits to the public mirror

**The public history is append-only.** `3ad7269` ("Initial public release")
squashed away the pre-launch dev history **once** and is now a permanent base.
From here on, every private change reaches the public repo as an ordinary new
commit **on top** of what is already there. Never squash, rebase, amend, or
`push --force` the public `main` — a fork or clone must never have its history
rewritten under it. The two repos therefore share file *content* but not commit
SHAs.

The one exception: a deliberate, user-approved hygiene fix for something that
should never have been public in the first place (e.g. the 2026-09-05
`Co-Authored-By:` leak below) — verify forks/watchers/stars first (GitHub API),
get explicit sign-off, then rewrite and force-push. That is not a sync step;
it does not change how routine syncs work.

### One-time setup

```sh
cd ~/cadence-public
git remote add private ~/cadence                       # local path, never pushed
git config remote.origin.pushurl git@github.com:Johlansl/cadence.git
git fetch private                                      # needed before the next line: the
                                                        # target commit isn't local yet otherwise
git update-ref refs/mirror/private-head 1c08096        # private commit the seed matches
```

`refs/mirror/private-head` only records how far the mirror has got. It is a
local convenience: it is not pushed and a fresh clone will not have it. It is
**not** at risk from `git gc` — every ref under `refs/` (not just `refs/heads`
and `refs/tags`) is a reachability root, `gc` never deletes refs, and
`git pack-refs` just moves it into `.git/packed-refs` (verified with
`gc --prune=now --aggressive`). It can still be lost to a manual `.git` edit or
a re-clone; the authoritative record is the `Mirrored-from:` trailer on each
public commit (see "If the bookmark is lost" below).

### Each sync

```sh
cd ~/cadence-public
git fetch private
git log --oneline "$(git rev-parse refs/mirror/private-head)"..private/main
```

Replay that range **one private commit at a time, in order**. Each private
commit becomes one public commit carrying a `Mirrored-from: <full private sha>`
trailer.

**Strip any `Co-Authored-By:` trailer before it lands on the public commit.**
This is not optional. On 2026-09-05 a `Co-Authored-By: Claude Sonnet 5
<noreply@anthropic.com>` trailer rode along from `~/cadence` straight into 3
public commits (mirroring preserves the original message, and the source
commits carried it), making an AI assistant show up in the public repo's
Contributors list. Fixed with `git filter-branch --msg-filter` + a one-off
`push --force` (repo had 0 forks/watchers/stars, verified via the GitHub API
first) — see the git log around 2026-09-05 for the incident commits. The
step below exists so it can't recur silently:

- **Touches only shared files:**
  ```sh
  git cherry-pick <sha>
  msg=$(git log -1 --format=%B | grep -vxF "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>")
  git commit --amend -m "$msg" --trailer "Mirrored-from: $(git rev-parse <sha>)"
  ```
- **Also touches a private-only file** (`.gitlab-ci.yml`, `CLAUDE.md`,
  `MAINTAINING.md`) — apply the diff with those paths filtered out, keeping the
  original message/author/date, then strip the trailer the same way:
  ```sh
  git -C ~/cadence show <sha> -- . \
    ':(exclude).gitlab-ci.yml' ':(exclude)CLAUDE.md' ':(exclude)MAINTAINING.md' \
    | git apply --index --3way
  git commit -C <sha>
  msg=$(git log -1 --format=%B | grep -vxF "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>")
  git commit --amend -m "$msg" --trailer "Mirrored-from: $(git rev-parse <sha>)"
  ```
- **Touches only private-only files** (e.g. an edit to this file) — skip it,
  nothing to mirror.

Then move the bookmark and push, fast-forward only:

```sh
git -C ~/cadence-public update-ref refs/mirror/private-head private/main
git -C ~/cadence-public push origin main:main          # SSH key ~/.ssh/id_ed25519
```

If `git push` reports a non-fast-forward, **stop and investigate** — something
rewrote the public `main`. Do not `--force`.

### If the bookmark is lost

Rebuild it from the public history, which is authoritative and survives a
re-clone:

```sh
cd ~/cadence-public && git fetch private
# newest Mirrored-from: trailer on public main
last=$(git log -1 --format=%B origin/main | sed -n 's/^Mirrored-from: *//p')
git update-ref refs/mirror/private-head "$last"
```

If public `main` predates the trailer convention, find the match by tree
instead — the private commit whose content (minus the three private-only
files) equals public `main`:

```sh
for sha in $(git rev-list --reverse 1c08096..private/main); do
  git diff --quiet origin/main "$sha" -- . \
    ':(exclude).gitlab-ci.yml' ':(exclude)CLAUDE.md' ':(exclude)MAINTAINING.md' \
    && { echo "public main == private $sha"; break; }
done
```

Either way, run the sanity check below before trusting the rebuilt bookmark.

### Sanity check after a sync

Every shared tracked file must be byte-identical in both repos:

```sh
cd ~/cadence && diff \
  <(git ls-files -s | grep -vxE '.*\t(\.gitlab-ci\.yml|CLAUDE\.md|MAINTAINING\.md)') \
  <(git -C ~/cadence-public ls-files -s) && echo OK
```

**If that diff is ever non-empty on a path outside the three private-only
files: stop.** Do not edit either repo to make it match. Print the diff, run
`git log` for the offending path on both sides to find which commit introduced
the drift, and work out the cause — a sync step skipped, a commit replayed
twice, or a manual edit made straight in `~/cadence-public`. Fix that cause,
not the symptom. There is no automated reconciliation, by design.

`rsync` is not installed on the host, hence the git-native replay above rather
than a tree copy. The `grep` exclude list here is the single source of truth
for the intended divergence — keep it equal to the "Intended divergence"
section.

Poll GitHub Actions:

```sh
curl -s "https://api.github.com/repos/Johlansl/cadence/actions/runs?per_page=3"
```

## Cutting a release

`.github/workflows/release.yml` runs on **GitHub** when a tag is pushed there.
So a release tag comes *after* the normal mirror sync, and is created on
`~/cadence-public` (like `v0.1.0` was), not on GitLab:

```sh
# after `main` is synced and both CIs are green:
git -C ~/cadence-public tag -a <tag> -m '<message>'
git -C ~/cadence-public push origin <tag>
# watch the run:
curl -s "https://api.github.com/repos/Johlansl/cadence/actions/runs?event=push&per_page=3"
```

- **`agent-v<x.y.z>`** — builds the agent (amd64+arm64) **binaries and `.deb`s**
  + Release. Prereq: an `## <x.y.z>` heading already exists in
  `agent/CHANGELOG.md` (the workflow fails if the notes section is missing).
  This is also the tag `scripts/publish-agent.sh` reads via `git describe`, so
  mirror it back to GitLab too (`git push …gitlab… <tag>`) to keep the private
  tree's `git describe` honest. Nothing extra to do for the `.deb` — it is
  built from `packaging/nfpm.yaml` and the (sed-rewritten) `agent/systemd/`
  units automatically. Plain `x.y.z` tags only; a `-rc*` suffix is not handled
  yet (see the backlog).
- **`v<x.y.z>`** — builds+pushes the GHCR images + Release. Prereq, in a normal
  commit merged and synced first: bump `backend/app/__init__.py` `__version__`
  and `frontend/package.json` `version` to `<x.y.z>`, and rename `CHANGELOG.md`
  `## Unreleased` → `## <x.y.z>`. The workflow asserts the tag matches those two
  files. Server tags stay public-only.
- The two are independent — never a combined tag. Push whichever the change
  warrants; push both (two runs) to cut both at once.
- First-ever `v*` run: after it succeeds, flip the new
  `ghcr.io/johlansl/cadence-{backend,frontend}` packages from private to public
  once (GitHub → your packages → Package settings → Change visibility).

## Release tarballs (separate concern)

`git archive` honours the `export-ignore` attributes in `.gitattributes`
(`.github/`, `.gitattributes`, `.gitignore`) to keep repo-meta out of release
tarballs. That list is independent of the mirror exclude list above.
