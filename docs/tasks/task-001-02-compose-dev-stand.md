# Задача 001.02: Docker Compose для разработки и стенда

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-10 Восстановление Control Plane из резервной копии (среда)
- UC-01 Ввод ноды в эксплуатацию (среда)

Требования RTM: R-42.

<!-- contract:goal -->

## Цель задачи

Поднять PostgreSQL 18, Redis 8 и nginx одной командой для окружений «разработка» и «стенд» по
`docs/architectures/deployment.md` §10.1.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `deploy/compose/docker-compose.yml` — базовые службы: `postgres` (18, WAL-архивация включена), `redis` (8, `appendonly yes`), `nginx` (1.30 — по `docs/architectures/stack.md`; запись плана «1.26» устарела), `api`, `worker-critical`, `worker-background`, `scheduler`
- `deploy/compose/docker-compose.dev.yml` — переопределения для разработки: порты наружу, монтирование исходников
- `deploy/compose/.env.example` — переменные без секретов: домены, режимы, адреса служб
- `deploy/compose/secrets/README.md` — перечень файлов Docker secrets: `pg_password`, `app_encryption_key`, `ca_key`, `smtp_password`, `pgbackrest_key`; добавлены при реализации по ролям data-model §4.6: `pg_app_rw_password` (приложение под `app_rw`), `pg_app_migrate_password` (миграции под `app_migrate`), `pg_app_backup_password` (копии под `app_backup`, добавлен в 001.03)
- `deploy/scripts/dev-secrets.sh` — генерация секретов и dev CA/сертификатов для разработки (добавлено при реализации: без него стенд не поднимается одной командой, AC-19)
- `deploy/scripts/vm-sync.sh`, `skills/vm-deploy/SKILL.md` — стенд живёт на VM Ubuntu, Docker на рабочей машине не ставится (решение пользователя при реализации)
- `deploy/nginx/nginx.conf` — три `server`: публичный (кабинет, панель, домены подписки), агентский с `ssl_verify_client on`, enrollment без клиентского сертификата; `map` для исключения `/s/` из журнала
- `control-plane/Dockerfile` — образ C-01…C-03 на `python:3.14-slim`, запуск через переменную `APP_ROLE`

### Интеграция компонентов

Compose-файлы читают секреты из `deploy/compose/secrets/`; nginx проксирует в `api:8000`. Файл
секретов вне репозитория по `.gitignore`.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Стенд поднимается одной командой
   - Входные данные: `docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml up -d`
   - Ожидаемый результат: все контейнеры в состоянии `healthy`; `GET /healthz` через nginx возвращает 200
2. **TC-E2E-02:** Путь подписки не журналируется
   - Входные данные: запрос `GET /s/test`
   - Ожидаемый результат: строка запроса отсутствует в `access.log` nginx

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml config`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] `docker compose up -d` завершается без ошибок дважды подряд на чистом хосте (AC-19)
- [ ] PostgreSQL запущен с `archive_mode = on`
- [ ] Redis запущен с `appendonly yes`
- [ ] Агентский `server` nginx требует клиентский сертификат; enrollment-`server` — нет

## Примечания

Промышленный профиль и pgbackrest — задача 001.66 и 001.67.

Зависимости: 001.01. Приоритет: Critical. Оценка: 4 ч. Этап: 0 — репозиторий и стенд.
