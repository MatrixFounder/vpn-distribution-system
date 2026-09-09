---
name: vm-deploy
description: DevOps on the dev/test Parallels Ubuntu VM (ssh vm) for vpn-distribution-system — sync the repo, run the Control Plane Compose stand (postgres, redis, nginx, app roles), inspect logs, run the stand's end-to-end checks. Adapted for this project from onchain-analytics vm-deploy.
tier: 2
version: 1.0
---

# VM Deploy (vpn-distribution-system)

**Purpose**: standardize DevOps on the dev/test VM — the Docker host for the **Control Plane
stand** (`deploy/compose/`). **Source of truth is this git repo**; the VM is a runtime, not a place
to edit. **The Mac never gets Docker.** VPN nodes (Xray + node-agent) are **not** deployed to this
VM — they go to remote VPS (task 001.61); the VM hosts only the management side (API, workers,
scheduler, nginx, PostgreSQL, Redis, admin panel).

## 1. Red Flags (Anti-Rationalization)

STOP if you are thinking:

- "I'll install Docker / Colima / OrbStack on the Mac" → **NO.** Docker runs on the VM only.
  Always `ssh vm`.
- "I'll edit a file on the VM and re-run" → source of truth is this repo. Edit locally, then
  `deploy/scripts/vm-sync.sh`, then run.
- "I'll `docker compose down -v` to get a clean state" → destroys the stand's PostgreSQL and
  Redis volumes. **Explicit user confirmation first** (§5), except inside a test that the user
  asked for (AC-19 clean-host check) and that says so in its report.
- "The api / worker / scheduler containers are red, the stand is broken" → check the log first.
  Until tasks 001.10 (`app.main`) and 001.11 (`app.jobs.worker`, `app.jobs.scheduler`) land, the
  app roles exit with `No module named …` / `Could not import module "app.main"` and `api` is
  `unhealthy`. That is the expected Stub-First state; infrastructure must still be `healthy`.
- "I'll touch supabase-*, n8n-*, onchain-*, job-dashboard, skills-mcp" → another project's
  containers on the same VM. **Never** — not even `restart`.
- "I'll use `docker compose logs` to prove a path is absent from nginx access.log" → compose
  multiplexes stdout and stderr; use `docker logs <container> 2>/dev/null` (access log) and
  `docker logs <container> 2>&1 >/dev/null` (error log) separately.
- "I changed nginx.conf, synced, and ran `nginx -s reload`" → the file is a single-file bind
  mount; rsync replaces it by rename, so the container still holds the **old inode** and reload
  re-reads the old config (proven 2026-09-08: `stat -c %i` differs on host and in container).
  After any change to a bind-mounted file (`nginx.conf`) run
  `$C up -d --force-recreate nginx`, then `nginx -T` to confirm the new text is live.

## 2. Connection

- **SSH alias `vm`** (in `~/.ssh/config` → Parallels VM, user `parallels`, key
  `id_ed25519_parallels`). **Never hardcode the IP**; when an address is needed, derive it:
  `ssh -G vm | awk '/^hostname /{print $2}'`.
- Smoke test: `ssh vm 'echo ok && docker ps --format "{{.Names}}" | grep control-plane'`
- Client address as the app sees it: `curl` from the VM host to a published port arrives via the
  Docker bridge, so nginx's `$remote_addr` (and therefore the app's client IP and the `rl:*:ip:`
  keys) is the bridge gateway (`172.20.0.1` on 2026-09-09), not `127.0.0.1`. Check with
  `redis-cli --scan --pattern "rl:*"` before clearing or reasoning about per-IP counters.
- VM facts (2026-09-08): Ubuntu 24.04 aarch64, Docker 28.5, Compose v5.4, 4 CPU, 7.7 GiB RAM,
  ~35 GB free. Ports **already taken by other projects**: 3000, 5432, 5433, 5678, 6543, 8000,
  8009, 8443, 9000 — never bind them.

## 3. What lives on the VM

| Path / container | Role | Our access |
| --- | --- | --- |
| `~/vpn-distribution-system/` | rsync copy of this repo (`deploy/scripts/vm-sync.sh`) | write via sync only |
| `~/vpn-distribution-system/deploy/compose/.env` | VM-local ports and modes; **not** in the repo | edit on the VM only, see §4 |
| `~/vpn-distribution-system/deploy/compose/secrets/` | dev secrets + dev CA (`dev-secrets.sh`) | regenerate with the script; never copy elsewhere |
| `control-plane-postgres-1`, `control-plane-redis-1`, `control-plane-nginx-1` | stand infrastructure | full |
| `control-plane-api-1`, `control-plane-worker-*-1`, `control-plane-scheduler-1` | app roles (`APP_ROLE`) | full |
| volumes `control-plane_pg_data`, `control-plane_redis_data` | stand data | `down -v` only with confirmation |
| everything else (`supabase-*`, `n8n-*`, `onchain-*`, `job-dashboard`, `skills-mcp`, …) | other projects | **DO NOT TOUCH** |

The VM `.env` differs from `.env.example` only in ports that collide with the other projects:
`PG_HOST_PORT=15432`, `REDIS_HOST_PORT=16379`, `AGENT_PORT=9443`, `ENROLL_PORT=9444`,
`DEV_BIND_ADDR=0.0.0.0`, `APP_ENV=dev`. nginx keeps 80 and 443.

## 4. Stand operations (the important part)

All commands run from the repo root on the Mac; `C` is the compose invocation used everywhere:

```bash
deploy/scripts/vm-sync.sh                      # 1. push the working tree (rsync, --delete)
ssh vm 'cd vpn-distribution-system && C="docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml"; \
  $C config -q && $C up -d --build'            # 2. validate, build the image, start
ssh vm 'cd vpn-distribution-system && docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml \
  ps -a --format "table {{.Service}}\t{{.Status}}"'   # 3. state
```

First-time setup on a fresh VM (once): copy `.env.example` to `.env` with the port overrides from
§3 (`chmod 600`), then generate dev secrets with the VM address in the certificate SAN:

```bash
ssh vm 'cd vpn-distribution-system && DEV_TLS_SAN="IP:$(hostname -I | cut -d" " -f1),DNS:vm" \
  deploy/scripts/dev-secrets.sh'
```

Logs (one service, last lines): `ssh vm 'docker logs control-plane-api-1 --tail 50 2>&1'`.
One-off command inside a role (runs as `app`, secrets readable):
`ssh vm 'cd vpn-distribution-system && … $C run --rm --no-deps -T api sh -c "id -u; ls /run/secrets"'`.
SQL: pipe over stdin, never `-f /tmp/…` (that reads the container FS):
`ssh vm 'docker exec -i control-plane-postgres-1 psql -qU postgres -d control_plane -v ON_ERROR_STOP=1' < control-plane/migrations/0001.sql`
(superuser `postgres` is for bootstrap only; after task 001.03 migrations run as `app_migrate` through `app.cli migrate`).

### End-to-end checks of the stand (task 001.02; rerun after every compose/nginx change)

| Check | Command on the VM | Expected |
| --- | --- | --- |
| infrastructure healthy | `$C ps` | `postgres`, `redis`, `nginx` = healthy |
| `/healthz` via nginx | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/healthz` | 200 once 001.10 is in; 502 before |
| `/s/` not logged (Н-25) | `curl -s http://127.0.0.1/s/test >/dev/null; docker logs control-plane-nginx-1 2>/dev/null \| grep -c /s/test; docker logs control-plane-nginx-1 2>&1 >/dev/null \| grep -c /s/test` | `0` and `0` |
| WAL archiving | `$C exec -T postgres psql -U postgres -d control_plane -Atc 'show archive_mode' -c 'select archived_count, failed_count from pg_stat_archiver'` | `on`; `failed_count = 0` |
| Redis persistence | `$C exec -T redis redis-cli config get appendonly` | `yes` |
| agent port needs mTLS | `curl -sk -o /dev/null -w '%{http_code}' https://127.0.0.1:9443/agent/v1/heartbeat` | `400` |
| agent port with dev cert | `… --cert deploy/compose/secrets/dev/dev-node.crt --key …/dev/dev-node.key …` | `502` before 001.10, then the API answer |
| log filter survives URL tricks | `curl --path-as-is -s http://127.0.0.1//s/t >/dev/null; curl --path-as-is -s http://127.0.0.1/x/../s/t >/dev/null;` then the two `docker logs … \| grep -c /s/t` counts | `0` and `0` |
| enrollment port, no cert | `curl -sk -o /dev/null -w '%{http_code}' -X POST https://127.0.0.1:9444/agent/v1/enroll` | proxied (`502` before 001.10); any other path `404` |
| agent API hidden on public port | `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/agent/v1/heartbeat` | `404` |
| secrets readable by the role | `$C run --rm --no-deps -T api sh -c 'id -u; for f in /run/secrets/*; do [ -r "$f" ] && echo "$f $(wc -c < "$f") bytes"; done'` | `10001`, then every file with a non-zero size (never print the contents) |
| database roles | `docker exec control-plane-postgres-1 psql -U postgres -d control_plane -Atc "select rolname, rolsuper from pg_roles where rolname like 'app_%'"` | four `app_*` roles, none superuser (created by `initdb.d/10-roles.sh`) |
| migrations applied at api start | `docker logs control-plane-api-1 2>&1 \| grep migrate:` | `migrate: применено — N, всего в источнике — N` before the uvicorn lines |

### Tests from the Mac against the stand's database

`control-plane/tests/conftest.py` reads `PG_DSN` / `REDIS_URL`. Point them at the VM:

```bash
VM_IP=$(ssh -G vm | awk '/^hostname /{print $2}')
PG_DSN="postgresql://app_rw:app@$VM_IP:15432/control_plane" \
MIGRATE_DSN="postgresql://app_migrate:app@$VM_IP:15432/control_plane" \
REDIS_URL="redis://$VM_IP:16379/0" make test-py
```

(dev passwords are `app`; the roles are created by `initdb.d/10-roles.sh` on a fresh volume —
an old volume needs `down -v` (confirm first) or the manual SQL from `secrets/README.md`.)

## 5. Safety Boundaries

- **Allowed scope**: `~/vpn-distribution-system` on the VM, containers and volumes named
  `control-plane*`, the compose commands above.
- **NEVER without explicit user confirmation**: `docker compose down -v` / `docker volume rm`
  on `control-plane_*` (the stand's data); `docker system prune`; `rm -rf` outside
  `~/vpn-distribution-system`; `reboot` / `shutdown`; `apt`, `systemctl`; anything touching
  other projects' containers, volumes, or ports (§3).
- MUST single-quote remote commands containing `{{…}}` templates or `$C`:
  `ssh vm 'docker ps --format "{{.Names}}"'`.
- Secrets stay on the VM; never `cat` a key into a transcript, report, or commit.
- `psql` run inside the postgres container against `127.0.0.1` uses `trust` (`pg_hba` of the
  image) and never checks a role's password; to prove a password works, connect through the
  published port from the Mac (`psycopg`/`psql` to `$VM_IP:15432`).

## 6. Rationalization Table

| Agent excuse | Reality |
| --- | --- |
| "docker compose config passed, so the stand works" | `config` validates YAML and interpolation only. Bring it up and run §4 checks; a stand that was never started is `NOT RUN`, not green. |
| "Compose secrets with `mode: 0444` fix the permission error" | Non-swarm Compose bind-mounts secret files with **host** permissions and ignores `mode`; the image's entrypoint copies them for `app` — do not work around it with `chmod 644` on the host. |
| "The path is absent from `docker compose logs`, so it is not logged" | Compose merges both streams; the access log is the container's stdout, the error log its stderr. Check each. |
| "I'll expose the agent port on 8443 like the docs say" | 8443 is Supabase Kong on this VM. Ports are `.env` values, not constants — the VM `.env` uses 9443/9444. |
