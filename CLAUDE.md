# Project

Project-specific agent instructions live in this file.

The agentic-development framework is imported via `CLAUDE.local.md`.

## Infra & VM ops (dev/test)

Docker на рабочей машине не устанавливается. Стенд Control Plane (Compose из `deploy/compose/`)
живёт в Parallels-VM Ubuntu — доступ **`ssh vm`** (IP не хардкодить). Для любой операции с VM,
Docker или стендом следуй скиллу **`vm-deploy`** (`skills/vm-deploy/SKILL.md`): репозиторий —
источник истины, на VM только `deploy/scripts/vm-sync.sh`; деструктивные действия (`down -v`,
`prune`, чужие контейнеры) — только с явного подтверждения. VPN-ноды на VM не ставятся —
только на удалённые VPS.
