# Отчёт о проверке — задача 001.84 «Сессии в Redis, CSRF, вход и ограничения частоты входа»

Дата: 2026-09-09 (раунд 2 после ревью). Задача закрыта без нового прикладного кода: её
контракт выполнен задачей 001.14 (`tests/tests-001/report-001-14.md`, четыре раунда Sarcasmotron,
коммит `d162aab`). Здесь — проверка соответствия, один добавленный страж (CSRF на
`logout-all`, ревью раунда 1) и собственный прогон регрессии.

## Регрессия

- `cd control-plane && pytest tests/e2e/test_auth.py -k 'login or session' tests/unit/security`
  (стенд VM, живые контейнеры) → **15 passed, 46 deselected**.
- `make check` этой задачи (nvm use 24, `PG_DSN`/`MIGRATE_DSN`/`REDIS_URL` → стенд) → код 0:
  ruff, ruff format, mypy strict (80 файлов), pytest **214 passed**, go, web.

## Соответствие контракта

| Пункт 001.84 | Где выполнено | Проверка |
| :--- | :--- | :--- |
| `SessionStore` на Redis, TTL, набор сессий пользователя, `revoke_all` | `app/security/sessions.py` (001.14) | `unit/security/test_sessions.py` — 4 теста (tdd-strict), посадки 10, 20, 21 отчёта 001.14 |
| `require_csrf` по `X-CSRF-Token` | `app/security/csrf.py` (001.14) | `test_logout_requires_csrf_and_revokes_sessions` — с раунда 2 без заголовка и с подделанным для **обоих** `logout` и `logout-all` (сессии целы); посадка 9 отчёта 001.14 и посадка ниже |
| Вход с argon2id, `auth_events` | `app/domain/users.py::authenticate` (001.14) | `test_uc02_full_registration_cycle`, `test_authenticate_costs_the_same_for_unknown_address` |
| Лимиты входа по адресу и по учётной записи, CAPTCHA вместо блокировки | `app/api/auth.py::login`, `security/ratelimit.py` (001.14, раунды 2–4) | TC-E2E-01 — `test_uc02_a5_login_rate_limit_by_ip_does_not_block_account`; `…never_locks_owner_without_provider`, `…requires_captcha_with_provider`, `…login_ip_threshold_offers_captcha_with_provider`, `test_one_captcha_answer_serves_both_login_thresholds`; посадки отчёта 001.14: 2, 8, 17–19 (раунд 2), 24–26 (раунд 3), 27–28 (раунд 4) |
| `logout-all` | `app/api/auth.py::logout_all` | TC-E2E-02 — `test_logout_requires_csrf_and_revokes_sessions` (две сессии → обе недействительны) |
| Скользящее окно 5/60, шестой — ложь | `RateLimiter.check` | TC-UNIT-01 — `test_sixth_call_within_window_is_rejected` |
| Fail-closed без Redis | `redis_required` на `/auth` и `/me`; `RedisConnectionError`/`TimeoutError` → 503 | `test_security_fail_closed.py` |

## Отклонения от формулировок задачи (объявлены в «Уточнениях» задачи)

- Окно скользящее на `ZSET` (Lua), а не `INCR`/`EXPIRE` с фиксированной границей.
- Имена ключей — по построителям 001.12 и §5.12 (`rl:login:account:*`, `rl:register:ip:*`,
  `rl:reset:email:*`); запрос восстановления ограничивается по адресу почты (§5.12), а
  `rl:reset:ip` из контракта живёт на `reset-confirm` (`TOKEN_IP`, 20/600 с).
- Порог по учётной записи считает только отказы; без провайдера CAPTCHA не действует (ОВ-A3,
  security.md §7.3).
- R-45 закрыт только в части входа; пороги subscription-токенов — 001.42, Node API и полный
  перечень §5.12 — 001.72, backoff агента — 001.54 (объявлено в задаче).

### Вердикт раунда 2

`sarcasmotron-001-84`: **APPROVED**. Посадки ревьюера: `require_csrf` снят с `logout_all` →
`204 == 403`; сравнение с токеном сессии заменено проверкой «заголовок непустой» → красная на
подделанном заголовке. N-5 (указатель 001.24 → 001.72/001.54) исправлен сразу.

## Раунд 2 — правки по ревью

Ревью раунда 1 (`sarcasmotron-001-84`, REJECTED): B-1 CSRF на `logout-all` не охранялся ни
одним тестом (посадка ревьюера — `require_csrf` снят только с `logout_all` — 214 passed);
M-2 пользовательская половина Н-27 (блокировка → отзыв сессий ≤ 60 с) не имела исполнителя;
M-1 висячие ссылки «посадки 24–28»; N-1 несуществующие построители `rl:verify:ip`/`rl:reset:ip`;
N-2 умолчание о `rl:reset:ip` на `reset-confirm`; N-3 отчёт без собственного полного прогона;
N-4 R-45 заявлен целиком.

- **B-1:** `test_logout_requires_csrf_and_revokes_sessions` проверяет без заголовка и с
  подделанным оба маршрута `logout` и `logout-all`, обе сессии целы после отказов. Посадка
  ревьюера повторена → `204 == 403` на `logout-all`.
- **M-2:** вызов `revoke_all` при блокировке пользователя записан в контракт 001.50
  (`/admin/users`, блокировка) с тестом «две живые сессии → блокировка → обе 401»; критерий
  Н-27 в задаче переформулирован: механизм — здесь, вызов — 001.50, 12 ч администратора — 001.47.
- **M-1:** посадки раундов 3–4 отчёта 001.14 пронумерованы (24–28). **N-1/N-2/N-4:** текст
  задачи и отчёта исправлен. **N-3:** собственный `make check` — 214 passed.

## Критерии приёмки

- [x] AC-36 для входа — 001.14.
- [x] Сессии инвалидируются в срок Н-27 — механизм `revoke_all` немедленный; вызов при
      блокировке пользователя — 001.50; 12 ч бездействия администратора — 001.47.
