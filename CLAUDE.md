# Cadence — brief projet

## 1. Contexte

Cadence est un outil de patch management léger pour serveurs Linux. Point de
départ : usage personnel sur un homelab Proxmox VE (VMs 100% Debian), avec une
architecture pensée pour évoluer vers un parc plus large et d'autres
distributions (Ubuntu, RHEL) plus tard. Possibilité de publication open
source si l'outil fonctionne bien, potentiellement open-core ensuite — mais
**la V1 est un outil mono-utilisateur, pas un produit multi-tenant.**

Problème résolu : les outils existants sont soit trop lourds pour un homelab
(Uyuni : 16-32 Go RAM, OS hôte imposé, DNS/FQDN obligatoire), soit incomplets
(Patchman : visibilité seule, pas de déploiement), soit une UI par-dessus
Ansible sans vraie vision patch management (Semaphore/AWX). Cadence vise la
visibilité (mini RHEL Satellite) **et** l'application des mises à jour, avec
un dashboard.

Déploiement cible : une VM Debian 12 sur Proxmox, 2 vCPU / 2 Go RAM, serveur
central en docker-compose. Les VMs surveillées sont d'autres VMs Debian sur
le même Proxmox, chacune avec l'agent installé.

## 2. Portée V1 — à respecter strictement

**Inclus :**
- Agent Go : collecte paquets installés + mises à jour disponibles via apt,
  distinction sécurité/normale, report périodique au serveur (auth par token
  simple par agent)
- Backend : réception des reports, état par host/paquet dans le temps,
  endpoint de déclenchement de mise à jour sur un host donné
- Dashboard : liste des hosts avec statut, détail par host des paquets à
  mettre à jour, bouton de déclenchement manuel
- Exécution des mises à jour : l'agent lance apt upgrade/dist-upgrade et
  remonte le résultat

**Explicitement hors scope V1 (ne pas implémenter, même si ce serait facile) :**
- Planification automatique / fenêtres de maintenance
- Règles d'exclusion de paquets
- Séquencement multi-hosts des reboots
- Notifications (Slack/mail)
- Multi-distribution (RPM etc.) — le modèle de données doit rester
  extensible pour ça, mais aucun code RHEL/dnf en V1
- Authentification multi-utilisateur / RBAC

Si une tâche donnée touche à un de ces points, s'arrêter et me demander avant
d'aller plus loin plutôt que d'anticiper.

## 3. Décisions d'architecture (déjà tranchées)

- **Agent Go, stdlib uniquement.** Pas de dépendance externe (pas de yaml,
  pas de framework CLI) : config via variables d'environnement, un seul
  binaire statique (`CGO_ENABLED=0`). Objectif : rester "léger" et facile à
  cross-compiler.
- **Agent one-shot, pas de démon.** Le binaire collecte, envoie un report,
  puis quitte. L'ordonnancement se fait via un `systemd timer`, pas via une
  boucle Go maison. Plus simple, plus observable (`journalctl`).
- **Communication outbound-only.** L'agent contacte le serveur, jamais
  l'inverse. Pas de long-poll : la réponse du `POST /api/v1/reports` peut
  transporter un job en attente pour ce host (piggyback), ce qui évite un
  mécanisme de connexion longue à maintenir côté backend.
- **Auth simple par token.** Un token par host, généré côté serveur à la
  création du host, transmis une seule fois. Seul le hash (sha256) est
  stocké en base — jamais le token en clair. Un header `X-Admin-Key`
  (secret partagé unique) protège les endpoints de provisioning admin.
- **Backend FastAPI + SQLAlchemy (sync) + PostgreSQL.** Pas d'Alembic pour
  la V1 : le schéma est appliqué via un `init.sql` monté dans
  `/docker-entrypoint-initdb.d/` au premier démarrage de Postgres. Alembic
  sera introduit quand le schéma devra évoluer sur une base existante.
- **Frontend React + Vite + Tailwind.** Pas de librairie de routing pour la
  V1 (une seule vue maître-détail, gérée par state React simple). Rafraîchi
  par polling (~30s), pas de websocket.
- **Déploiement docker-compose** sur la VM serveur : services `db`,
  `backend`, `frontend`. TLS (reverse proxy type Caddy) recommandé mais pas
  bloquant pour ce premier increment — à signaler si on veut l'ajouter.
- **Détection sécurité vs normale (apt) :** simulation `apt-get -s
  dist-upgrade`, parsing des lignes `Inst ...`, heuristique sur la présence
  de "security" dans la chaîne d'origine du paquet. Documenté comme
  heuristique, pas une vérité absolue.
- **Détection reboot-required :** présence du fichier
  `/var/run/reboot-required` (Debian/Ubuntu). Modélisé comme un booléen
  générique en base pour ne pas coupler le schéma à cette implémentation.
- **Comparaison de versions :** jamais faite côté backend/Go — on fait
  confiance à apt pour dire "il y a une mise à jour", jamais de comparaison
  sémantique de version maison.
- Extensibilité déjà prévue dans le schéma sans sur-ingénierie : colonne
  `hosts.tags` (jsonb, groundwork pour des groupes en V2), colonne
  `hosts.package_manager` (permet dnf plus tard), table `jobs` avec colonne
  `params` (jsonb) pour les futures options (exclusions, fenêtres) et
  colonne `requested_by` (texte libre aujourd'hui, deviendra une FK
  `user_id` avec le RBAC).

## 4. Schéma de données (à utiliser tel quel comme `init.sql`)

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE hosts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    hostname            TEXT NOT NULL,
    fqdn                TEXT,
    description         TEXT,
    token_hash          TEXT NOT NULL UNIQUE,
    os_family           TEXT NOT NULL DEFAULT 'debian',
    os_name             TEXT,
    os_version          TEXT,
    package_manager     TEXT NOT NULL DEFAULT 'apt',
    agent_version       TEXT,
    tags                JSONB NOT NULL DEFAULT '{}'::jsonb,
    reboot_required     BOOLEAN NOT NULL DEFAULT false,
    is_active           BOOLEAN NOT NULL DEFAULT true,
    last_seen_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE packages (
    id           BIGSERIAL PRIMARY KEY,
    name         TEXT NOT NULL,
    architecture TEXT NOT NULL DEFAULT '',
    UNIQUE (name, architecture)
);

CREATE TABLE host_packages (
    host_id             UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    package_id          BIGINT NOT NULL REFERENCES packages(id) ON DELETE CASCADE,
    installed_version   TEXT NOT NULL,
    candidate_version   TEXT,
    is_security_update  BOOLEAN NOT NULL DEFAULT false,
    update_origin       TEXT,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (host_id, package_id)
);

CREATE INDEX idx_host_packages_pending_updates
    ON host_packages (host_id) WHERE candidate_version IS NOT NULL;

CREATE TABLE reports (
    id                       BIGSERIAL PRIMARY KEY,
    host_id                  UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    received_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_version            TEXT,
    installed_package_count  INTEGER NOT NULL DEFAULT 0,
    updates_available_count  INTEGER NOT NULL DEFAULT 0,
    security_updates_count   INTEGER NOT NULL DEFAULT 0,
    reboot_required          BOOLEAN NOT NULL DEFAULT false,
    raw_payload              JSONB NOT NULL
);

CREATE INDEX idx_reports_host_received ON reports (host_id, received_at DESC);

-- Table posée maintenant pour stabilité du modèle, mais AUCUN endpoint ne
-- doit la manipuler en V1 -- voir section 8, étape 8.
CREATE TABLE jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    host_id        UUID NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    job_type       TEXT NOT NULL DEFAULT 'apt_upgrade',
    status         TEXT NOT NULL DEFAULT 'pending',
    params         JSONB NOT NULL DEFAULT '{}'::jsonb,
    requested_by   TEXT,
    result         JSONB,
    log            TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    completed_at   TIMESTAMPTZ
);

CREATE INDEX idx_jobs_host_status ON jobs (host_id, status);
CREATE INDEX idx_jobs_pending ON jobs (host_id) WHERE status = 'pending';
```

## 5. Contrat API V1

- `POST /api/v1/admin/hosts` — header `X-Admin-Key`. Body
  `{hostname, fqdn?, description?}`. Crée le host, retourne
  `{id, hostname, token}` (token en clair, une seule fois).
- `POST /api/v1/reports` — header `Authorization: Bearer <token>`. Body :
  voir payload agent ci-dessous. Remplace l'état courant des paquets du
  host, journalise le report brut.
- `GET /api/v1/hosts` — liste avec statut calculé
  (`up_to_date` / `updates_available` / `security_updates_available`).
- `GET /api/v1/hosts/{id}` — détail avec la liste des paquets.
- `GET /healthz`.

Payload de report envoyé par l'agent (JSON) :

```json
{
  "agent_version": "0.1.0",
  "hostname": "vm-web-01",
  "fqdn": null,
  "os_family": "debian",
  "os_name": "Debian GNU/Linux",
  "os_version": "12",
  "package_manager": "apt",
  "reboot_required": false,
  "packages": [
    {
      "name": "openssl",
      "architecture": "amd64",
      "installed_version": "3.0.11-1~deb12u2",
      "candidate_version": "3.0.13-1~deb12u1",
      "is_security_update": true,
      "update_origin": "Debian-Security:12/stable-security"
    }
  ]
}
```

## 6. Arborescence cible

```
cadence/
├── agent/
│   ├── cmd/agent/main.go
│   ├── internal/{config,collector,client,report}/
│   ├── systemd/{cadence-agent.service,cadence-agent.timer}
│   └── go.mod
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── core/config.py
│   │   ├── db/{base.py,init.sql}
│   │   ├── models/models.py
│   │   ├── schemas/schemas.py
│   │   └── api/{deps.py,routes/{reports.py,hosts.py,admin.py}}
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/
│   ├── src/{api/client.ts,components/,App.tsx,main.tsx}
│   ├── package.json
│   └── Dockerfile
├── docker-compose.yml
└── README.md
```

## 7. Notes d'implémentation

**Agent :** collecte via `dpkg-query -W -f='${Package}\t${Architecture}\t${Version}\n'`
pour les paquets installés, `apt-get -s dist-upgrade` pour les mises à jour
disponibles (parser les lignes `Inst <pkg> [<old>] (<new> <origin>)`),
`/etc/os-release` pour les infos OS, `/var/run/reboot-required` pour le
reboot. Config par variables d'env : `CADENCE_SERVER_URL`, `CADENCE_TOKEN`,
`CADENCE_RUN_APT_UPDATE` (bool), `CADENCE_HTTP_TIMEOUT_SECONDS`.

**Frontend :** liste des hosts avec badges de statut sémantiques (vert = à
jour, ambre = mises à jour dispo, rouge = mises à jour de sécurité), et un
indicateur de fraîcheur du dernier report par host (utile pour repérer un
agent qui ne reporte plus). Police monospace pour les noms de host/paquets/
versions (lisibilité des données techniques). Éviter le look "SaaS
générique" par défaut de Tailwind — un dashboard d'admin sobre, pas une
landing page.

## 8. Plan de travail — un incrément testable à la fois, dans l'ordre

1. Scaffolding docker-compose + `init.sql` + service `db` seul. Valider :
   `docker compose up db` démarre proprement, tables visibles via `psql`.
2. Backend minimal : `/healthz`, `POST /api/v1/admin/hosts`,
   `POST /api/v1/reports`. Tester au `curl` (payload à la main), sans
   frontend ni agent.
3. Backend : `GET /api/v1/hosts` et `GET /api/v1/hosts/{id}`. Tester au
   `curl`.
4. Agent Go : collecte + report, testé avec `--server-url`/env pointant sur
   le backend en local, exécuté manuellement sur une VM Debian réelle du
   homelab. Vérifier en base que les données arrivent correctement.
5. Unit + timer systemd pour l'agent, installés sur la VM homelab.
   Vérifier le cycle automatique via `journalctl -u cadence-agent`.
6. Frontend minimal : liste + détail, connecté au backend.
7. Validation bout en bout sur au moins deux VMs réelles du homelab.
8. **Ne pas commencer avant validation explicite de ma part** : endpoints
   de création/déclenchement de jobs, exécution `apt upgrade` côté agent,
   remontée des logs de job.

## 9. Règles pour toi (Claude Code)

- Ne pas ajouter de dépendance externe non mentionnée ici sans le signaler
  d'abord — en particulier côté agent Go, stdlib strictement.
- Ne pas implémenter les points listés en section 2 "hors scope V1", même
  si ça semble trivial en cours de route. Un TODO/commentaire suffit.
- Un incrément = une étape de la section 8, testable indépendamment. Pas de
  gros commit qui mélange plusieurs étapes.
- Avant toute modification du schéma en section 4, proposer le changement
  et pourquoi plutôt que de l'appliquer directement.
- Code (identifiants, commentaires) en anglais même si nos échanges sont en
  français — cohérence en prévision d'une éventuelle publication open
  source.
- Environnement cible : VM Debian 12, 2 vCPU / 2 Go RAM, docker-compose. Ne
  pas supposer d'outillage supplémentaire (pas de Kubernetes, pas de
  registre d'images privé, etc.).

## 10. Point de départ

Commence par l'étape 1 de la section 8. Une fois chaque étape validée, je
te donnerai le feu vert pour la suivante.
