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

- `node-agent/internal/app/app.go` — `func Run(ctx context.Context, cfg Config) error` — цикл: enroll → sync → reports
- `node-agent/internal/config/config.go` — `type Config struct { EnrollURL, AgentURL string; IdentityDir string; XrayAPI string; ReportInterval, HeartbeatInterval time.Duration }`; загрузка из `/etc/node-agent/config.yaml`
- `node-agent/internal/cpclient/client.go` — `type Client interface { Enroll(ctx, req) (EnrollResp, error); State(ctx, cursors) (StateResp, error); Ack(ctx, cfg, seq) error; Heartbeat(ctx) error; Report(ctx, r) (ReportResp, error); QuotaRequest(ctx, userID, consumed) (Grant, error); Metrics(ctx, m) error; CommandResult(ctx, id, res) error }` — HTTP-клиент с mTLS; заглушка
- `node-agent/internal/xray/client.go` — `type Xray interface { AddUser(ctx, tag, user) error; RemoveUser(ctx, tag, email) error; QueryStats(ctx, pattern) ([]Stat, error); OnlineIPs(ctx, email) ([]net.IP, error); AddRule(ctx, rule) error; RemoveRule(ctx, tag) error }` — gRPC; заглушка
- `node-agent/internal/store/store.go` — `type Store interface { SaveReport(r) error; PendingReports() ([]Report, error); DeleteReport(seq) error; Epoch() (string, error); SetEpoch(string) error; Rules() ([]Rule, error) }` — SQLite; заглушка
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
- `node-agent/internal/guard/routing.go` — `Apply(blocked []net.IP) error`, `Reapply() error` — заглушки
- `node-agent/internal/guard/nftables.go` — `Render(params) string`, `Load(ruleset string) error` — заглушки
- `node-agent/internal/guard/conntrack.go` — `Stats() ([]ConnStat, error)` — заглушка
- `node-agent/internal/metrics/collect.go` — `Collect() (Metrics, error)` — заглушка
- `node-agent/internal/disconnect/strategies.go` — `type Strategy interface { Disconnect(ctx, user) error }`; `ByName(name) (Strategy, error)` — заглушки

### Интеграция компонентов

Фикстуры контракта общие с Control Plane (001.28). Заглушки возвращают значения фикстур.

<!-- contract:tests -->

## Тестовые случаи

### Сквозные тесты

1. **TC-E2E-01:** Контракт агента
   - Входные данные: фикстуры `state`, `reports`
   - Ожидаемый результат: структуры разбираются без ошибок; неизвестное поле игнорируется; отсутствующий обязательный раздел → ошибка `ErrUnknownSection`
   - Примечание: заглушка

### Модульные тесты

1. **TC-UNIT-01:** Загрузка конфигурации
   - Проверяемая функция: `internal/config::Load`
   - Входные данные: файл с двумя адресами
   - Ожидаемый результат: оба адреса разобраны; отсутствие enroll-адреса — ошибка

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

Зависимости: 001.01, 001.28. Приоритет: Critical. Оценка: 4 ч. Этап: 9 — Node Agent (Go).
