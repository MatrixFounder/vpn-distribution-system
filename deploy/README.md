# deploy/

Артефакты развёртывания по `docs/architectures/deployment.md`.

| Каталог | Назначение | Задача плана |
| :--- | :--- | :--- |
| `compose/` | `docker-compose.yml` (стенд) + `docker-compose.dev.yml` (разработка); `.env.example`; `secrets/README.md` — перечень Docker secrets и сертификатов | 001.02, 001.66 |
| `compose/postgres/` | Обёртка точки входа postgres и скрипт initdb.d, создающий роли базы из `control-plane/migrations/bootstrap/roles.sql` | 001.03 |
| `nginx/` | Обратный прокси: TLS для публичных доменов, `server` агентов с mTLS, `server` enrollment без клиентского сертификата, исключение `/s/` из журнала | 001.02, 001.66 |
| `node/` | Bootstrap-скрипт ноды, юниты systemd для Xray и Node Agent, базовый набор nftables, контрольная сумма Xray-core | 001.61 |
| `prometheus/` | Конфигурация Prometheus и Alertmanager, правила алертов §17.4 постановки | 001.68 |
| `scripts/` | `dev-secrets.sh` — секреты и сертификаты для разработки; `vm-sync.sh` — копирование дерева на VM стенда; восстановление из копии | 001.02, 001.67 |

Секреты (`compose/secrets/`) и `.env` в репозиторий не попадают — см. `.gitignore`.

## Стенд одной командой (окружение «Разработка», §10.1)

```sh
cp deploy/compose/.env.example deploy/compose/.env && chmod 600 deploy/compose/.env
deploy/scripts/dev-secrets.sh
docker compose --env-file deploy/compose/.env \
  -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.dev.yml up -d --build
```

Порты на хосте задаёт `.env` (`HTTP_PORT`, `AGENT_PORT`, `PG_HOST_PORT`, …). Роли приложения
(`api`, `worker-*`, `scheduler`) стартуют, когда появится `app.main` (задача 001.10); до этого
`docker compose ps` показывает их завершившимися, а `GET /healthz` через nginx отвечает 502.
Docker на рабочей машине не ставится: стенд живёт на VM Ubuntu — см. `skills/vm-deploy/SKILL.md`.
