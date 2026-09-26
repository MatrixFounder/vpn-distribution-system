# tests/

Здесь **отчёты** о проверке задач, не код тестов: `tests-001/report-001-NN.md` — по одному на
задачу плана 001 (результаты `make check`, стенд, посадки регрессий, раунды ревью, вердикт).

Код тестов живёт рядом с приложением и выполняется `make check` и CI:

| Что | Где |
| :--- | :--- |
| Сквозные тесты Control Plane (миграции по группам схемы, очередь задач, каркас API, аутентификация, fail-closed) | `control-plane/tests/e2e/` |
| Модульные тесты (каталоги схемы, конфигурация, контракт прокси, очередь, безопасность) | `control-plane/tests/unit/` |
| Общие фикстуры (стенд, клиент ASGI, окружение `Settings`) | `control-plane/tests/conftest.py` |
| Карта тестов по файлам | раздел `[tests/]` в `control-plane/.AGENTS.md` |

Запуск против стенда (VM, см. `skills/vm-deploy/SKILL.md`):

```sh
deploy/scripts/stand-tunnel.sh start   # база и Redis стенда — только на 127.0.0.1 VM
export PG_DSN=postgresql://app_rw:app@127.0.0.1:15432/control_plane \
       MIGRATE_DSN=postgresql://app_migrate:app@127.0.0.1:15432/control_plane \
       REDIS_URL=redis://127.0.0.1:16379/0
source ~/.nvm/nvm.sh && nvm use 24 && make check
```

Тесты миграций откатывают схему на стенде через миграцию 130, а её откат на непустой
`node_identities` отказывает (он стёр бы серийные номера листов, а повторное применение не прошло
бы NOT NULL). Identity тестовых нод — сквозных тестов (`t19-`) и проб стенда (`t25-`), в том числе
брошенные прерванным прогоном, — помощник отката снимает сам
(`control-plane/tests/e2e/_cli.py::clear_test_identities`); identity нетестовой ноды остаётся, и
тест отказывает с их числом — её снимают вручную до прогона. Поэтому `make check` и пробу стенда
(`control-plane/tests/stand/enrollment_probe.py`) одновременно не запускают: откат через 130 снял
бы identity идущей пробы.
