# Задача 001.53: Node Agent: каркас, конфигурация, клиенты, хранилище — интерфейсы

Тип задачи: `[STUB CREATION]`.

## Связь со сценариями
- UC-01 Ввод ноды в эксплуатацию
- UC-04 Учёт трафика и списание с коэффициентом

Требования RTM: R-02, R-04, R-05, R-19, R-20, R-25, R-26, R-27, R-28.

<!-- contract:goal -->

## Цель задачи

Объявить все пакеты `internal/*` по `docs/ARCHITECTURE.md` §1.1 с интерфейсами и заглушками.
Включая `enroll`, `sync`, `accounting`, `quota`, `guard`, `metrics`, `disconnect`. Контрактные тесты
против фикстур `contracts/agent_v1/` компилируются и проходят на фиксированных ответах; логические
задачи этапа 9 изменяют существующие файлы.

<!-- contract:changes -->

## Описание изменений

### Новые файлы

- `node-agent/internal/config/config.go` — `type Config struct { EnrollURL, AgentURL string; ServerCAFile string; IdentityDir string; XrayAPI string; HeartbeatInterval time.Duration }`; загрузка из `/etc/node-agent/config.yaml`; `ServerCAFile` — якорь серверов из
  bootstrap-команды (security.md §7.1, 001.61); интервал отчёта — константа 60 с в `accounting`
  (§5.9, Н-17), а не параметр конфигурации
- `node-agent/internal/cpclient/client.go` — `type Client interface { Enroll(ctx, req) (EnrollResp, error); State(ctx, cursors) (StateResp, error); Ack(ctx, cfg, seq) error; Heartbeat(ctx, hb HeartbeatIn) error; Report(ctx, r) (ReportResp, error); QuotaRequest(ctx, userID, consumed) (Grant, error); Metrics(ctx, m) error; CommandResult(ctx, id, res) error }` — HTTP-клиент с mTLS; заглушка. Грант квоты (`Grant` и
  грант строки состава) хранит `quota_grant_bytes` указателем: `null` — грант без ограничения
  тарифа Unlimited (изменение контракта agent_v1 в 001.36, В-6), а не 0
- `node-agent/internal/xray/client.go` — `type Xray interface { AddUser(ctx, tag, user) error; RemoveUser(ctx, tag, email) error; QueryStats(ctx, pattern) ([]Stat, error); OnlineIPs(ctx, email) (map[netip.Addr]time.Time, error); Uptime(ctx) (time.Duration, error); AddRule(ctx, rule) error; RemoveRule(ctx, tag) error }` — gRPC; `OnlineIPs`
  возвращает адрес и `last_seen` (поля отчёта); `Uptime` — время работы процесса Xray
  (`GetSysStats`) для распознавания перезапуска; заглушка. Методов `AddInbound`/`RemoveInbound`
  нет: стратегия `restart_inbound` только замеряется в PoC-1а (001.88) и выбрана быть не может
  (решение заказчика 2026-10-25, В-9)
- `node-agent/internal/store/store.go` — `type Store interface { SaveReport(r) error; PendingReports() ([]Report, error); DeleteReport(epoch string, seq int64) error; Epoch() (string, error); SetEpoch(string) error; Rules() ([]Rule, error) }` — SQLite; номер части уникален
  только в пределах эпохи, поэтому удаление — по паре `(epoch, seq)`; заглушка
- `node-agent/internal/contracts/contracts_test.go` — контрактные тесты по фикстурам `contracts/agent_v1/*.json`
- `node-agent/internal/.AGENTS.md` — карта пакетов
- `node-agent/internal/enroll/enroll.go` — `func Enroll(ctx, cfg, token string) (Identity, error)` — заглушка
- `node-agent/internal/sync/loop.go` — `func Run(ctx, deps) error` — заглушка одной итерации
- `node-agent/internal/sync/apply_config.go` — `func ApplyConfig(ctx, cfg StateConfig) error` — заглушка
- `node-agent/internal/sync/apply_users.go` — `func ApplyUsers(ctx, rows []UserRow, full bool) error` — заглушка
- `node-agent/internal/sync/commands.go` — `func Execute(ctx, cmd Command) Result` — заглушка `expired`
- `node-agent/internal/accounting/collector.go` — `func Collect(ctx) (Report, error)` — заглушка
- `node-agent/internal/accounting/buffer.go` — `type Buffer` над Store — заглушка
- `node-agent/internal/accounting/sender.go` — `func Flush(ctx) error` — заглушка
- `node-agent/internal/accounting/clock.go` — `func Skew(ctx) (time.Duration, error)` — заглушка
- `node-agent/internal/quota/quota.go` — `type Tracker`: `OnDelta`, `RequestIfNeeded`, `OnExhausted` — заглушки
- `node-agent/internal/guard/routing.go` — `Apply(user string, blocked []netip.Addr) error`, `Reapply() error` — заглушки
- `node-agent/internal/guard/nftables.go` — `Render(params) string`, `Load(ruleset string) error` — заглушки
- `node-agent/internal/guard/conntrack.go` — `Stats() ([]ConnStat, error)` — заглушка
- `node-agent/internal/metrics/collect.go` — `Collect() (Metrics, error)` — заглушка
- `node-agent/internal/disconnect/strategies.go` — `type Strategy interface { Disconnect(ctx, user) error }`; `ByName(name) (Strategy, error)` — заглушки

### Изменения в существующих файлах

#### Файл: `node-agent/internal/app/app.go`

- `func Run(ctx context.Context, cfg Config) error` (файл создан в 001.01) — цикл: enroll → sync →
  reports

### Интеграция компонентов

Фикстуры контракта общие с Control Plane (001.28). Заглушки возвращают значения фикстур.
Фикстура `enroll.json` — настоящая выдача: CSR, лист и CA в ней согласованы.

Реализации интерфейсов: `xray/client.go` — 001.56 (`StatsService`), 001.80 (`HandlerService`),
001.58 (`RoutingService`); `store/store.go` — 001.55 (схема и миграции SQLite, курсоры), остальные
задачи этапа 9 дополняют его своими таблицами. Метод клиента для операции ротации identity
`POST /agent/v1/identity/rotate` (контракт — 001.31, В-16), поле `output` результата команды
(контракт — 001.31, В-18) и локальную
подкоманду `node-agent decommission` (В-21) добавляет 001.81.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Контракт агента
   - Входные данные: фикстуры `state`, `reports`
   - Ожидаемый результат: структуры разбираются без ошибок; неизвестное поле и неизвестный раздел
     игнорируются; отсутствующий известный обязательный раздел → ошибка `ErrMissingSection`
   - Примечание: заглушка. Новый обязательный раздел вводит новая версия пути (`/agent/v2`), агент
     v1 его не получает; признака неизвестного раздела в heartbeat и перевода ноды в `degraded`
     нет (решение заказчика 2026-10-25, В-14)

### Модульные тесты

1. **TC-UNIT-01:** Загрузка конфигурации
   - Проверяемая функция: `internal/config::Load`
   - Входные данные: файл с двумя адресами и путём якоря серверов
   - Ожидаемый результат: оба адреса и путь якоря разобраны; отсутствие enroll-адреса или якоря —
     ошибка

### Регрессионные тесты

- Команда: `cd node-agent && go test ./...`
- Полный набор: `make test` — существующие тесты проходят.

<!-- contract:acceptance -->

## Критерии приёмки

- [ ] Все пакеты §1.1 и `disconnect` объявлены с сигнатурами этой задачи
- [ ] `go vet` и `golangci-lint` чисты
- [ ] Контрактные тесты проходят на заглушках

## Примечания

Ограничения и допущения — `docs/idea.md` §9; архитектура — `docs/ARCHITECTURE.md`.

Найдено при 001.33 (заглушки), решить здесь:

- фикстуры `contracts/agent_v1/report*.json`, `quota-request*.json` и раздел «Отчёт о трафике и
  грант квоты» README — контракт отчёта для стороны Go: канонические формы (UUID 36 символов,
  метки RFC 3339 по грамматике с `T` и смещением до 32 символов, адреса без zone id, целые —
  только JSON-числа без дробной части), единицы в именах полей, компактная запись без отступов,
  экранирование ASCII в строках не применять; деление отчёта на части (фикстура продолжения —
  `report-continuation.json`); правила отправителя отчётов (части, интервалы, эпохи, сетка
  снятия, отказы) — тело 001.56, реакции клиента на коды ответа, форма тела запроса и таймауты —
  тело 001.54; сжатие тела запроса контрактом не предусмотрено — ввести или нет, решить здесь по
  измерениям (прокси и приложение `Content-Encoding` не разбирают).


Найдено при 001.25 (enrollment и identity), решить здесь:

- ключ ноды — только EC P-256 (`prime256v1`): CSR с другим ключом CA отвергает 422
  `invalid_csr` (токен при этом не гасится — можно повторить с годным CSR); субъект и расширения
  CSR CA не копирует — лист всегда `CN=<node_id>`, `CA:FALSE`, `clientAuth`, 90 дней, но не
  дальше срока самого CA (срок листа агент читает из сертификата, а не считает сам);
- отказы обмена — 401 с кодом: `token_invalid` (не выдавался или нода выведена), `token_used`
  (уже обменян — сигнал оператору: обменял кто-то другой, UC-01 шаг 6), `token_expired` (истёк
  или аннулирован — новым токеном или отзывом identity; нужен новый токен); повтор с тем же
  токеном бессмысленен во всех трёх;
- после обмена агент печатает SHA-256 DER своего листа (hex нижнего регистра — форма
  `node_identities.cert_fingerprint`): на шаге 6 UC-01 администратор сверяет его с состоянием
  ноды в панели — иначе сверять отпечаток не с чем.

Зависимости: 001.01, 001.28. Приоритет: Critical. Оценка: 4 ч. Этап: 9 — Node Agent (Go).
