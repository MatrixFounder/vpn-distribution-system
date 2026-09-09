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
VM_IP=$(ssh -G vm | awk '/^hostname /{print $2}')
export PG_DSN=postgresql://app_rw:app@$VM_IP:15432/control_plane \
       MIGRATE_DSN=postgresql://app_migrate:app@$VM_IP:15432/control_plane \
       REDIS_URL=redis://$VM_IP:16379/0
source ~/.nvm/nvm.sh && nvm use 24 && make check
```
