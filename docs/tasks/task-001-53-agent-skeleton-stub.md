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

Найдено при 001.33 (заглушки), решить здесь:

- фикстуры `contracts/agent_v1/report*.json`, `quota-request*.json` и раздел «Отчёт о трафике и
  грант квоты» README — контракт отчёта для стороны Go: канонические формы (UUID 36 символов,
  метки RFC 3339 по грамматике с `T` и смещением до 32 символов, адреса без zone id, целые —
  только JSON-числа без дробной части), единицы в именах полей, компактная запись без отступов,
  деление отчёта на части (интервал `(counter_epoch, period_start, period_end)`, пользователь в
  одной части, счётчики интерфейса в первой; фикстура продолжения —
  `report-continuation.json`), интервалы ноды монотонны и не перекрываются (и через смену
  эпохи), `period_end` не опережает часы сервера, два смысла 403 с разными `Retry-After`, 429
  от прокси в едином формате ошибки с `Retry-After: 1`, 413 — страница nginx без тела единого  формата, **411 `length_required`** единого формата на тело без известной длины (`Transfer-Encoding`
  при любом методе, поток HTTP/2 без content-length — не повторять: тело шлётся с
  `Content-Length`), `Content-Type: application/json` обязателен (без него — 422); тело каждой
  операции сверяется по байтам с формой её модели до разбора — лишнее поле у плоских тел даёт
  422 `too_wide`, синтаксически негодный JSON — 422 `json_invalid` (оба чинятся на стороне
  агента, не повторяются и не делятся); 503 `upstream_unavailable` от прокси с `Retry-After` —
  повторить; перезапуск Xray посередине интервала: прежняя эпоха закрывается последним снятым
  интервалом, новая начинается с момента старта Xray (иначе с момента обнаружения) до ближайшей
  минутной границы, интервалы эпох не перекрываются; перезапуск агента при живом Xray эпоху не
  меняет; один запрос отчёта в полёте на ноду независимо от числа действующих сертификатов (в
  окне ротации их два); сжатие тела запроса контрактом не
  предусмотрено — ввести или нет, решить здесь по измерениям (прокси и приложение
  `Content-Encoding` не разбирают); тело отправлять с `Content-Length`
  (`bytes.Reader`/`ContentLength`), экранирование ASCII в строках не применять; повтор после
  429 — через `Retry-After` **плюс случайную добавку до секунды**,
  начало интервалов отчёта — со случайной фазой; штатный интервал 60 с (§5.9/Н-17 —
  обязанность агента), не дольше часа только после перерыва снятия счётчиков, перерыв дольше
  часа — новая эпоха; снятие счётчиков на границе часа UTC — рекомендация README; `parts_total`
  в каждой части — объявленное число, которое в пределах интервала только растёт; нумерация
  частей после деления отказанной по размеру — тот же номер, `parts_total` последующих частей
  увеличивается на число добавленных; после отказа `rejected_time` или `interval_closed`
  (часть к закрытому интервалу: закрывается завершением, частью следующего интервала или через
  25 часов после последней части) часть не повторяется, номер остаётся пропущенным, агент
  продолжает с `max(last_accepted_seq, номер отказанной) + 1`; 400 и `too_wide` — тело чинится
  на стороне агента, не повторяется; таймауты: отчёт — время заливки при полосе ноды плюс 10 с
  (Н-4 считается по времени апстрима, сквозное время законной части больше 200 мс),  остальные операции — не меньше 30 с (очередь парка на прокси — 200 при пятнадцати в секунду,
  14 с), long-poll состояния — не меньше 60 с (очередь плюс удержание); повтор после 429 — с
  джиттером.


Найдено при 001.25 (enrollment и identity), решить здесь:

- ключ ноды — только EC P-256 (`prime256v1`): CSR с другим ключом CA отвергает 422
  `invalid_csr` (токен при этом не гасится — можно повторить с годным CSR); субъект и расширения
  CSR CA не копирует — лист всегда `CN=<node_id>`, `CA:FALSE`, `clientAuth`, 90 дней, но не
  дальше срока самого CA (срок листа агент читает из сертификата, а не считает сам);
- отказы обмена — 401 с кодом: `token_invalid` (не выдавался или нода выведена), `token_used`
  (уже обменян — сигнал оператору: обменял кто-то другой, UC-01 шаг 6), `token_expired` (истёк
  или аннулирован — новым токеном или отзывом identity; нужен новый токен); повтор с тем же
  токеном бессмысленен во всех трёх;
- **якорь доверия — из bootstrap-команды, а не из ответа** (роаст 001.25, раунд 2): панель выдаёт
  вместе с токеном сертификат CA (`BootstrapToken.ca_pem`), администратор переносит его на VPS
  своим каналом (001.61). Агент проверяет им серверный сертификат enrollment-server **до** того,
  как отдать токен (иначе токен и сам CA пришли бы по непроверенному TLS, и посредник на первом
  шаге обменял бы токен сам и подсунул агенту свой CA), и требует, чтобы `ca_pem` ответа обмена
  совпал с якорем; тем же CA — с проверкой имени и `extendedKeyUsage serverAuth` — проверяет
  серверный сертификат агентского порта (листы нод — только `clientAuth` и без SAN, поэтому
  украденный лист ноды не выдаст себя за Control Plane); клиентский сертификат предъявляется в
  TLS, токен identity — в `X-Node-Identity`; заголовок `X-Client-Cert` ставит прокси — агент его
  не шлёт (README `contracts/agent_v1`); фикстура `enroll.json` — настоящая выдача: CSR, лист и CA
  в ней согласованы;
- после обмена агент печатает SHA-256 DER своего листа (hex нижнего регистра — форма
  `node_identities.cert_fingerprint`): на шаге 6 UC-01 администратор сверяет его с состоянием
  ноды в панели — иначе сверять отпечаток не с чем.

Зависимости: 001.01, 001.28. Приоритет: Critical. Оценка: 4 ч. Этап: 9 — Node Agent (Go).
