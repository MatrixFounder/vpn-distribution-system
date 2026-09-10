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

## Статусы задач в плане

`docs/PLAN.md` хранит статус каждой задачи в строке `Статус:` её записи (`принята — коммит <hash>
(<дата>)`, `в работе (с <дата>)`, `не начата`). После приёмки задачи: обновить эту строку и
выполнить `python3 docs/scripts/plan_graph.py --write` — он пересобирает раздел «Состояние
выполнения и граф зависимостей» (таблица, готовые к началу, критический путь, диаграмма Mermaid)
и отмечает пункты чек-листа RTM; `make lint-plan` (часть `make lint`) падает, если блок отстал.
Следующая задача выбирается из списка «Готовы к началу», а не по номеру.
