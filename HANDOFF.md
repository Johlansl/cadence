# Cadence — Handoff de session (2026-08-28)

Contexte complet pour reprendre le projet dans une nouvelle session Claude Code.
À lire avec `CLAUDE.md` (le brief, fait autorité sur le périmètre) et `README.md`
(procédures de déploiement / test, tenu à jour à chaque étape).

---

## 1. Où on en est — TL;DR

- **Le plan `CLAUDE.md` section 8 (étapes 1 → 8) est terminé et validé.** La V1
  est fonctionnellement complète.
- Un vrai `apt dist-upgrade` a été déclenché depuis le dashboard et exécuté sur
  une VM réelle (`vm-japp`, Debian 13) — job `succeeded`, log remonté.
- Ajout post-V1 déjà livré : **poll de job à 60 s** pour que les upgrades
  déclenchés partent en ~1 min.
- Tout est poussé sur GitLab `Johlansl/cadence`, branche **`main`**, HEAD
  `6c28d33`. Working tree propre.
- Stack `db` + `backend` + `frontend` **tourne** sur cette VM (le serveur
  central = la machine de dev, `192.168.1.78`).

### Reprise rapide

```sh
cd ~/cadence
git pull
docker compose up -d --build          # si la stack n'est pas up
curl -s http://127.0.0.1:8000/healthz # {"status":"ok"}
# dashboard : http://192.168.1.78:8080/
```

Rien n'est « en cours » : la prochaine action est un **choix** (voir §9).

---

## 2. Ce qu'est Cadence (rappel)

Patch-management léger pour serveurs Linux (homelab Proxmox, VMs Debian).
Mono-utilisateur en V1. Trois composants :

- **Agent Go** (stdlib only, one-shot, piloté par systemd timer) : collecte les
  paquets installés + mises à jour apt, poste un report, et exécute un job
  d'upgrade si le serveur lui en donne un.
- **Backend** FastAPI + SQLAlchemy (sync) + PostgreSQL : ingère les reports,
  tient l'état par host/paquet, distribue les jobs, expose des vues lecture.
- **Frontend** React + Vite + Tailwind : dashboard maître-détail, polling,
  bouton de déclenchement d'upgrade.

Communication **outbound-only** : l'agent appelle le serveur, jamais l'inverse.

---

## 3. Architecture telle que construite

### Endpoints backend (`backend/app/api/routes/`)

| Méthode / route | Auth | Rôle |
|---|---|---|
| `GET /healthz` | — | liveness (ne touche pas la DB) |
| `POST /api/v1/admin/hosts` | `X-Admin-Key` | crée un host, renvoie `{id, hostname, token}` (token en clair, une fois ; seul le sha256 est stocké) |
| `POST /api/v1/admin/hosts/{id}/jobs` | `X-Admin-Key` | crée un job `apt_upgrade` ; **409** si un job est déjà `pending`/`running` pour ce host |
| `POST /api/v1/reports` | `Bearer <token>` | remplace l'état paquets du host, journalise le report brut, **piggyback** : la réponse porte `job` s'il y en a un en attente |
| `POST /api/v1/agent/next-job` | `Bearer <token>` | **poll rapide** : réclame le job `pending` le plus ancien → `running`, sans collecte. `{job: null}` si rien |
| `POST /api/v1/jobs/{id}/result` | `Bearer <token>` | l'agent remonte `{status, exit_code, log, reboot_required}` → `succeeded`/`failed` |
| `GET /api/v1/hosts` | — | liste + statut calculé (`up_to_date` / `updates_available` / `security_updates_available`) |
| `GET /api/v1/hosts/{id}` | — | détail + liste des paquets |
| `GET /api/v1/hosts/{id}/jobs` | — | 20 derniers jobs |
| `GET /api/v1/jobs/{id}` | — | un job (avec log) |

Les `GET` de dashboard sont **non authentifiés** (V1 mono-user, réseau de
confiance). Helper partagé `claim_pending_job()` dans `routes/jobs.py`
(utilisé par le piggyback et par `next-job`), avec `SELECT … FOR UPDATE SKIP
LOCKED` → un seul consommateur par job.

### Cycle de vie d'un job

`pending` → `running` (réclamé par report ou par poll) → `succeeded` | `failed`.
Un seul job actif par host. `jobs.params` (jsonb) est vide en V1, prévu pour
les options futures (fenêtres, exclusions, choix reboot).

### Agent — deux modes one-shot

| Invocation | Timer | Fait |
|---|---|---|
| `cadence-agent` | `cadence-agent.timer` — horaire | collecte (dpkg + `apt-get -s dist-upgrade` + os-release) → `POST /reports` → exécute le job piggybacké s'il y en a un |
| `cadence-agent -poll` | `cadence-agent-poll.timer` — **60 s** | `POST /agent/next-job` → exécute le job s'il y en a un ; sinon exit 0 silencieux |

`install.sh` pose les 4 units et active les 2 timers. Agent **v0.3.0**.

### Exécution d'un upgrade

`apt-get -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold -y dist-upgrade`
avec `DEBIAN_FRONTEND=noninteractive`, `NEEDRESTART_MODE=a`, `LC_ALL=C`.
Log combiné stdout+stderr. **Ne reboote jamais** — remonte seulement
`reboot_required` (présence de `/var/run/reboot-required`). Pas de `apt-get
update` implicite dans le job (on applique ce qu'apt connaît déjà ; la
fraîcheur des listes est pilotée côté collecte par `CADENCE_RUN_APT_UPDATE`).

### Frontend

`frontend/` — SPA servie par nginx, qui **proxy `/api/` → `backend:8000`**
(origine unique, pas de CORS). Vue unique maître-détail, polling 30 s (liste +
détail) et 15 s (jobs). Composants : `HostList`, `HostDetail`, `Jobs`,
`StatusBadge`, `Freshness`. Le bouton *trigger dist-upgrade* demande la
`X-Admin-Key` une fois → `sessionStorage` (`lib/adminKey.ts`).

### Config (variables d'env)

- **Backend** (`backend/app/core/config.py`) : `POSTGRES_*` construisent l'URL
  (ou `CADENCE_DATABASE_URL` direct), `CADENCE_ADMIN_KEY` (requis).
- **Agent** (`agent/internal/config/config.go`) : `CADENCE_SERVER_URL`,
  `CADENCE_TOKEN` (requis) ; `CADENCE_RUN_APT_UPDATE` (défaut false),
  `CADENCE_ENABLE_UPGRADES` (défaut true — kill-switch : job renvoyé `failed`
  si false), `CADENCE_HTTP_TIMEOUT_SECONDS` (défaut 30).
- **docker-compose** : `CADENCE_BACKEND_BIND` / `CADENCE_FRONTEND_BIND`
  (127.0.0.1 par défaut, `0.0.0.0` pour exposer sur le LAN). Ici les deux sont
  à `0.0.0.0`. `CADENCE_DEV_API` pour le proxy Vite en dev.

---

## 4. Arborescence

```
cadence/
├── CLAUDE.md                 brief (fait autorité) — §11 état, §12 backlog
├── HANDOFF.md                ce fichier
├── README.md                 procédures pas à pas (déploiement, tests)
├── docker-compose.yml        db + backend + frontend
├── .env.example  /  .env     (.env gitignoré, contient la vraie CADENCE_ADMIN_KEY)
├── backend/
│   ├── app/
│   │   ├── main.py           include des routers + /healthz
│   │   ├── core/config.py
│   │   ├── db/{base.py, init.sql}   ← init.sql = schéma DDL, appliqué au 1er boot
│   │   ├── models/models.py  ORM (Host, Package, HostPackage, Report, Job)
│   │   ├── schemas/schemas.py
│   │   └── api/{deps.py, routes/{admin,hosts,jobs,reports}.py}
│   ├── requirements.txt      fastapi, uvicorn[standard], sqlalchemy, psycopg2-binary, pydantic
│   └── Dockerfile
├── agent/                    module Go `cadence/agent`, go 1.23, zéro dépendance
│   ├── cmd/agent/main.go     modes normal / -poll
│   ├── internal/{config,collector,client,executor,report}/
│   │   └── collector/*_test.go, client/client_test.go
│   └── systemd/{cadence-agent.{service,timer}, cadence-agent-poll.{service,timer},
│                agent.env.example, install.sh}
└── frontend/
    ├── src/{main.tsx, App.tsx, api/client.ts, lib/{adminKey,time}.ts,
    │        components/*, types.ts, index.css}
    ├── nginx.conf            SPA + proxy /api → backend:8000
    ├── package.json / package-lock.json
    └── Dockerfile            node:22 build → nginx:1.27
```

Pas d'Alembic (V1 : schéma via `init.sql`). Toute évolution du schéma section 4
du brief se **propose d'abord**.

---

## 5. Journal de session — ce qui a été fait

Ordre des commits (`git log --oneline`) :

| Commit | Contenu |
|---|---|
| `2d81c36` | **Incréments 1-5** : docker-compose + `init.sql` ; backend `/healthz` + provisioning + ingestion reports ; vues `GET /hosts` (+ statut) ; agent Go (collecte dpkg/apt/os-release + report) ; units + timer systemd + `install.sh` |
| `a71b992` | **Incrément 6** : dashboard React maître-détail, badges de statut, fraîcheur du dernier report, table paquets |
| `c3fbf64` | **8a** — backend jobs : création (X-Admin-Key, 409 si actif), piggyback dans la réponse report, callback résultat, GET jobs |
| `3ae9955` | **8b** — agent 0.2.0 : lit le job piggybacké, `executor` (`apt dist-upgrade` non-interactif), remonte log + exit code, `CADENCE_ENABLE_UPGRADES` |
| `e413c7b` | **8c** — dashboard : bouton *trigger dist-upgrade*, historique jobs, log dépliable, `X-Admin-Key` en sessionStorage |
| `dc791ed` | doc : checklist steps 7-8 |
| `3e0cfc4` | **Post-V1** : poll de job dédié — `POST /agent/next-job`, mode `-poll`, `cadence-agent-poll.timer` (60 s), agent 0.3.0 |
| `6c28d33` | doc : `CLAUDE.md` §11 (état) + §12 (backlog) |

Chaque incrément a été testé (curl côté backend, `go test` + build conteneur
côté agent, `systemd-analyze verify`, build Vite + smoke via nginx côté front,
puis bout-en-bout sur `vm-japp`).

---

## 6. Décisions et justifications

| Décision | Pourquoi |
|---|---|
| **`apt-get dist-upgrade`** (pas `upgrade`) pour le job | applique tout ce que le dashboard affiche, noyaux inclus ; cohérent avec la détection qui utilise déjà `-s dist-upgrade` |
| **Jamais de reboot automatique** | choix explicite ; hors-scope V1 « séquencement des reboots » ; l'agent remonte juste `reboot_required` |
| **Trigger de job protégé par `X-Admin-Key`** | modifier un système mérite plus que les GET lecture ; front stocke la clé en `sessionStorage` |
| **Poll de job dédié** plutôt que long-poll / push / timer court | garde l'outbound-only et le one-shot (pas de démon), ~1 min de latence, sans re-simuler apt chaque minute ni polluer le log du report. Retenu comme « la bonne solution pour une distribution future » |
| **GET dashboard non authentifiés** | V1 mono-user sur réseau de confiance ; le contrat API ne prévoit pas d'auth dessus |
| **`claim_pending_job` + `FOR UPDATE SKIP LOCKED`** | le report horaire et le poll 60 s peuvent tomber en même temps → un seul réclame le job |
| **Agent : le hostname du report écrase celui saisi à la création** | l'agent fait foi sur son propre nom |
| **`packages` = table dimension partagée** | jamais supprimée ; `host_packages` est remplacé à chaque report |
| **Pas de comparaison de version maison** | on fait confiance à `apt` pour dire « il y a une MAJ » |
| **Backend : deps standard** (uvicorn, sqlalchemy, psycopg2-binary, pydantic) ; **agent : stdlib strict** (`flag`, `net/http`, `os/exec`, `regexp`…) | brief §3 / §9 |
| **`postgres:16`, `node:22-alpine`, `nginx:1.27-alpine`, Go 1.23** | versions stables, pas de raison de bouger |

Détection sécurité vs normale : **heuristique** — sous-chaîne `security` dans la
chaîne d'origine apt (`Debian-Security:13/stable-security` → oui). Documentée
comme telle.

---

## 7. État du déploiement

### Serveur central = cette VM (`vm-cadence`, `192.168.1.78`)

- Stack docker-compose **up** : `backend` sur `0.0.0.0:8000`, `frontend` sur
  `0.0.0.0:8080`, `db` sur `127.0.0.1:5432` (volume `pgdata`).
- `CADENCE_ADMIN_KEY` : dans `~/cadence/.env` (gitignoré). Générée avec
  `openssl rand -hex 32`.
- 1 host en base : **`vm-japp`** (`bf39558e-…`), agent **0.2.0**, 425 paquets,
  0 en attente (dist-upgrade appliqué), `reboot_required=false` (voir §8).
- 1 job : `b9caf3a0-…` `succeeded` (le dist-upgrade réel déclenché du dashboard).

### VM surveillée : `vm-japp` (Debian 13, homelab)

- Agent installé via `install.sh`, timer horaire actif.
- **Action en attente côté utilisateur** : déployer l'agent **0.3.0**
  (`git pull` + `go build` + `sudo systemd/install.sh`) pour activer le poll
  60 s, et `sudo apt install update-notifier-common` (voir §8).

### GitLab

- `https://gitlab.com/Johlansl/cadence`, privé, branche `main`.
- Push HTTPS avec un **PAT `write_repository`** que l'utilisateur a fourni pour
  la session (dans l'historique shell, pas dans le repo — `.git/config` propre).
  À révoquer / renouveler pour une prochaine session.
- `git config branch.main.merge` posé sans fetch (repo privé) — le premier
  `git fetch` résoudra `origin/main`.

---

## 8. Limites connues / pièges

1. **`reboot_required` reste `false` après une MAJ noyau** si
   `update-notifier-common` n'est pas installé sur la VM (Debian minimale ne
   pose pas `/var/run/reboot-required`). → `apt install update-notifier-common`
   sur les VMs surveillées.
2. **Piège mot de passe Postgres** : `POSTGRES_PASSWORD` n'est fixé qu'au 1er
   démarrage sur volume vide. Le changer ensuite → `password authentication
   failed` (backend 500). Fix : `docker compose down -v` (perte des données) ou
   `ALTER ROLE` dans Postgres.
3. **Un report avec `"packages": []`** remplace l'état paquets par vide (c'est
   le comportement voulu « remplace l'état courant »). Ne pas envoyer de report
   de test vide contre un vrai host.
4. **Job `running` bloqué** : si un agent réclame un job puis meurt sans poster
   de résultat, le job reste `running` (pas de timeout de reprise en V1).
5. **GET dashboard non authentifiés** : quiconque atteint le port 8080/8000 voit
   les hosts/paquets/jobs.
6. **Pas de TLS** : HTTP nu. OK sur LAN de confiance, à ne pas exposer.
7. **Pas de tests automatisés côté backend** (testé au curl uniquement).
   L'agent a `client_test.go` + `apt_test.go`.
8. Le nom de host affiché peut « sauter » : l'agent écrase le hostname saisi à
   la création par son vrai hostname OS.

---

## 9. Ce qui reste — options pour la suite

Aucune n'est « l'étape suivante » imposée : à choisir avec l'utilisateur.

| # | Sujet | Nature | Effort |
|---|---|---|---|
| 1 | **TLS / reverse proxy Caddy** devant backend+frontend | non-feature, recommandé depuis le début, non bloquant sur LAN | petit (service compose + Caddyfile) |
| 2 | Déployer agent **0.3.0** sur `vm-japp` + `apt install update-notifier-common` | action utilisateur | 2 min |
| 3 | Resserrer les seuils de fraîcheur du dashboard (`lib/time.ts`) — le poll rafraîchit `last_seen_at` chaque minute, on peut détecter un agent mort en ~5 min au lieu de ~90 min | polish | très petit |
| 4 | Tests automatisés backend (pytest sur l'API) | robustesse | moyen |
| 5 | 2ᵉ VM surveillée | validation parc | action utilisateur |
| 6 | Backlog §12 (choix reboot dashboard, planification) | features V2 | **feu vert requis** |

Reco : **#1 (TLS)** si on veut sortir du LAN de confiance / envisager la
publication ; sinon **#3** (rapide, rend l'indicateur de fraîcheur vraiment
utile).

---

## 10. Backlog explicitement différé (voir `CLAUDE.md` §12)

**Ne pas implémenter sans feu vert explicite de l'utilisateur :**

1. **Choix reboot côté dashboard** — `reboot = auto | never` (voire `prompt`)
   par job ou par host, stocké dans `jobs.params` (jsonb, déjà prévu).
   Aujourd'hui l'agent ne reboote jamais.
2. **Planification / fenêtres de maintenance** — timer automatique d'update ou
   jour du mois + heure. Un futur planificateur insère des lignes `jobs`,
   options de fenêtre dans `jobs.params`. C'est le point « planification
   automatique / fenêtres de maintenance » listé hors-scope en section 2 du
   brief.

---

## 11. Comment reprendre concrètement

```sh
cd ~/cadence && git pull

# stack serveur (sur cette VM)
docker compose up -d --build
curl -s http://127.0.0.1:8000/healthz
#   dashboard : http://192.168.1.78:8080/
#   admin key : dans ~/cadence/.env  (CADENCE_ADMIN_KEY)

# agent en local pour bidouiller (binaire statique, gitignoré) :
docker run --rm -v "$PWD/agent":/src -w /src golang:1.23 \
  sh -c 'go vet ./... && go test ./... && CGO_ENABLED=0 go build -trimpath -o bin/cadence-agent ./cmd/agent'

# front : build/verif
docker run --rm -v "$PWD/frontend":/app -w /app node:22-alpine \
  sh -c 'npm ci && npm run build'
```

Lecture d'orientation : `CLAUDE.md` (brief + §11 état + §12 backlog) → ce
fichier → `README.md` (procédures). Le code est commenté ; les décisions sont
en §6 ici.
