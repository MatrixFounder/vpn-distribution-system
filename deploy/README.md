# deploy/

Артефакты развёртывания по `docs/architectures/deployment.md`.

| Каталог | Назначение | Задача плана |
| :--- | :--- | :--- |
| `compose/` | `docker-compose` для окружений «разработка», «стенд», «промышленное»; `.env.example`; перечень Docker secrets | 001.02, 001.66 |
| `nginx/` | Обратный прокси: TLS для публичных доменов, `server` агентов с mTLS, `server` enrollment без клиентского сертификата, исключение `/s/` из журнала | 001.02, 001.66 |
| `node/` | Bootstrap-скрипт ноды, юниты systemd для Xray и Node Agent, базовый набор nftables, контрольная сумма Xray-core | 001.61 |
| `prometheus/` | Конфигурация Prometheus и Alertmanager, правила алертов §17.4 постановки | 001.68 |

Секреты (`compose/secrets/`) и `.env` в репозиторий не попадают — см. `.gitignore`.
