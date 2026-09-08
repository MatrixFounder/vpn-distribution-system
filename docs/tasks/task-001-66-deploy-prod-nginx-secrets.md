# Задача 001.66: Промышленный профиль Compose, nginx mTLS и enrollment, секреты, домен писем

Тип задачи: `[CONFIGURATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-03 Получение подписки клиентом

Требования RTM: R-42, R-44, R-41.

<!-- contract:goal -->

## Цель задачи

Довести развёртывание до §10.3–§10.4 архитектуры: промышленный Compose, три `server` nginx с
правилами границы mTLS, Docker secrets, ACME, DNS-записи доменов и домена писем (SPF, DKIM, DMARC).

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `deploy/compose/docker-compose.prod.yml` — без публикации портов C-01; секреты из файлов; `restart: unless-stopped`
- `deploy/nginx/conf.d/agent.conf` — `ssl_verify_client on`, `proxy_set_header X-Client-Fingerprint $ssl_client_fingerprint` (перезапись безусловно), отдельный `server` enrollment
- `deploy/README.md` — DNS-записи: кабинет, панель, два домена подписки, домен писем с SPF/DKIM/DMARC; ACME

### Изменения в существующих файлах

#### Файл: `deploy/nginx/nginx.conf`

- HSTS, CSP, TLS 1.2+; `map $request_uri $loggable` для `/s/`

### Интеграция компонентов

Правила границы 1–3 из `docs/architectures/system-architecture.md` C-09.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Заголовок отпечатка не принимается от клиента
   - Входные данные: запрос с поддельным `X-Client-Fingerprint` без сертификата
   - Ожидаемый результат: заголовок перезаписан; 401
2. **TC-E2E-02:** Развёртывание воспроизводимо
   - Входные данные: чистый хост, `docker compose up -d` дважды
   - Ожидаемый результат: обе попытки успешны (AC-19)

### Модульные тесты

- Не требуются: задача не содержит логики сверх заглушек или конфигурации.

### Регрессионные тесты

- Команда: `docker compose -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.prod.yml config && nginx -t -c deploy/nginx/nginx.conf`
- Полный набор: `make check` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] AC-19 выполнен
- [ ] C-01 недостижим в обход nginx
- [ ] Письмо подтверждения доходит в основную папку на трёх службах (AC-37, проверка на стенде)

## Примечания

Конфигурационная задача.

Зависимости: 001.02, 001.25. Приоритет: Critical. Оценка: 4 ч. Этап: 11 — эксплуатация.
