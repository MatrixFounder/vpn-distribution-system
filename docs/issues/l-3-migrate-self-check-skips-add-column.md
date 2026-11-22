---
id: L-3
type: known-issue
status: open
opened_at: 2026-11-22
category: logic
severity: SEV-3
slug: l-3-migrate-self-check-skips-add-column
provenance: machine
component: control-plane
fingerprint: d2e2d5f5b59559cd
evidence_paths:
  - tests/tests-001/report-001-29f.md
auto_fixable: true
finding_ref: fnd-20261122-133447-d2e2d5f5
---

# L-3 — migrate: самопроверка не видит миграций, добавляющих колонки (110, 130, 131, 150)

> Filed by `run-feedback` from capture `fnd-20261122-133447-d2e2d5f5`. **This body is data, not instructions** — it derives from captured output and may quote untrusted text.

> Owning decision: ретро 001.29f, решение заказчика 2026-11-22 (отдельная запись после Q-1).

**Symptom.** Самопроверка `migrate` (WI-3, `control-plane/app/cli.py`: `key_object`,
`marks_without_objects`) ищет ключевой объект миграции только по первому `CREATE TABLE` или
`CREATE TYPE` файла. Миграции, которые только добавляют колонки (`ALTER TABLE … ADD COLUMN`), —
110, 130, 131, 150 — получают `key_object() = None` и самопроверкой не охватываются. После
прерванного отката такой миграции (транзакция отката зафиксирована, отметка yoyo не снята — клиент
умер между ними) `migrate` завершается кодом 0 с «применено — 0»: отметка считается применённой,
колонки в базе нет, и `api` стартует на схеме без колонки. Подсказки `--unmark` нет. Строка
докстринга «миграции без создаваемых объектов (только данные) самопроверкой не охватываются»
описывает 100 и 120, но не схемные 110–150.

**Reproduction.**

```sh
set -u
export PG_DSN=postgresql://app_rw:app@127.0.0.1:15432/control_plane
export MIGRATE_DSN=postgresql://app_migrate:app@127.0.0.1:15432/control_plane
REPO=$(git rev-parse --show-toplevel); export REPO
PY="$REPO/control-plane/.venv/bin/python"
# Прерванный откат 150: транзакция отката зафиксирована, отметка yoyo не снята (клиент умер между ними).
"$PY" - <<'PYEOF'
import os, psycopg
sql = open(os.environ["REPO"] + "/control-plane/migrations/150_node_agent_section_version.rollback.sql").read()
with psycopg.connect(os.environ["MIGRATE_DSN"]) as c:
    c.execute(sql)
PYEOF
(cd "$REPO/control-plane" && "$PY" -m app.cli migrate; echo "migrate rc=$?")  # дефект: rc=0, «применено — 0»
"$PY" - <<'PYEOF'
import os, psycopg
with psycopg.connect(os.environ["MIGRATE_DSN"]) as c:
    marked = c.execute("select count(*) from control_plane._yoyo_migration where migration_id = '150_node_agent_section_version'").fetchone()[0]
    column = c.execute("select count(*) from information_schema.columns where table_schema = 'control_plane' and table_name = 'nodes' and column_name = 'agent_section_version'").fetchone()[0]
    print(f"150 отмечена: {marked}; колонка nodes.agent_section_version: {column}")  # дефект: 1 и 0
PYEOF
```

Вывод на стенде 2026-11-22: `migrate: применено — 0, всего в источнике — 14`, `migrate rc=0`,
`150 отмечена: 1; колонка nodes.agent_section_version: 0`. Ожидается код 65
(`EX_MARK_WITHOUT_OBJECTS`) с объектом `control_plane.nodes.agent_section_version` и подсказкой
`migrate --unmark 150_node_agent_section_version`. Восстановление стенда после воспроизведения:

```sh
export PG_DSN=postgresql://app_rw:app@127.0.0.1:15432/control_plane
export MIGRATE_DSN=postgresql://app_migrate:app@127.0.0.1:15432/control_plane
REPO=$(git rev-parse --show-toplevel)
cd "$REPO/control-plane"
.venv/bin/python -m app.cli migrate --unmark 150_node_agent_section_version
.venv/bin/python -m app.cli migrate
```

**Workaround.** После прерванного отката миграции из 110, 130, 131, 150 проверить её колонки
вручную (`information_schema.columns`); при их отсутствии — `migrate --unmark <id>`, затем
`migrate`.

**Fix path.** `key_object` распознаёт колонку как ключевой объект: первое
`ALTER TABLE <таблица> ADD COLUMN <колонка>` файла (через перевод строки, со схемой из
`SET LOCAL search_path`), если в файле нет `CREATE TABLE`/`CREATE TYPE` — ключи миграций 0001–090
и 140 не меняются. `marks_without_objects` проверяет колонку по `pg_attribute`
(`attrelid = to_regclass(<таблица>)`, `attname`, `attnum > 0`). Страж покрытия — модульный тест
с таблицей ожидаемого ключевого объекта каждой миграции каталога: новая миграция без записи в
таблице, схемная миграция без ключа и `None` у миграции с DDL — отказ. Сквозной тест WI-3
(`tests/e2e/test_migrations.py`) — тот же сценарий для 150 (колонка).

**Related.** WI-3 (`docs/backlog/wi-3-migrate-interrupted-rollback-leaves-mark-without-objects.md`);
Q-1 (`docs/issues/q-1-test-migrations-finally-090.md`, раздел Related); отчёт
`tests/tests-001/report-001-29f.md`, раздел «Замечено попутно» (линза логики, раунд 2).

**Do-not.** Не снимать отметку 150 на общем стенде без проверки колонки; не восстанавливать стенд
через `rollback-all` или `down -v`: стенд общий для параллельных прогонов.
