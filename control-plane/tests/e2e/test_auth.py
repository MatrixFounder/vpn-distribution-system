"""Сквозные проверки ``/api/v1/auth`` с настоящей логикой (задача 001.14) на живом стенде:
UC-02 шаги 1–6 с альтернативами A1 (адрес занят), A5 (порог частоты), A6/A7 (режимы
регистрации) и UC-15 с A1…A3; одноразовые домены; CAPTCHA без провайдера; CSRF на выходе.

TC-E2E-01: регистрация → письмо из очереди → подтверждение → вход; учётная запись подтверждена,
cookie сессии, ``auth_events`` содержит ``register`` и ``login``. TC-E2E-02: тот же адрес в другом
регистре → 409 ``email_taken``. TC-E2E-03: 6 неверных паролей с одного адреса → 6-й 429; учётная
запись не заблокирована. TC-E2E-04: две сессии → смена пароля по ссылке → обе недействительны,
повторный переход — 410. Структурные проверки 001.13 (семь маршрутов, схемы, валидация, cookie)
сохранены. Помощники и уборка — ``_auth.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import statistics
import time
from http.cookies import SimpleCookie

import httpx
import pytest
from app.api.auth import get_user_service
from app.domain.users import UserService, hash_token
from app.security import ratelimit
from app.security.sessions import SessionStore

from ._auth import PASSWORD, AuthStand, auth_stand

AUTH_ROUTES = {
    "/api/v1/auth/register": "RegisterIn",
    "/api/v1/auth/verify": "VerifyIn",
    "/api/v1/auth/login": "LoginIn",
    "/api/v1/auth/logout": None,
    "/api/v1/auth/logout-all": None,
    "/api/v1/auth/reset-request": "ResetRequestIn",
    "/api/v1/auth/reset-confirm": "ResetConfirmIn",
}
VALID_REGISTER = {
    "email": "new.user@example.com",
    "password": PASSWORD,
    "aup_version": "2026-09",
    "lang": "ru",
}
VOLATILE_HEADERS = {"date"}  # различаются между любыми двумя ответами по построению
ROUNDS = 12  # выборка времени ответа reset-request на каждый из двух случаев
TIMING_FLOOR_MS = 3.0  # шум стенда: сеть до VM и планировщик Python
TIMING_RATIO = 0.25


def cookie_attributes(header: str, name: str = "sid") -> tuple[str, dict[str, str]]:
    """Разобрать Set-Cookie: значение и атрибуты в нижнем регистре (без подстрочных догадок)."""
    jar: SimpleCookie = SimpleCookie()
    jar.load(header)
    assert name in jar, header
    morsel = jar[name]
    attributes = {
        key: str(value).lower() for key, value in morsel.items() if value not in ("", False)
    }
    return morsel.value, attributes


def cookies_of(response: httpx.Response) -> dict[str, tuple[str, dict[str, str]]]:
    return {
        header.split("=", 1)[0]: cookie_attributes(header, header.split("=", 1)[0])
        for header in response.headers.get_list("set-cookie")
    }


def stable_headers(response: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() not in VOLATILE_HEADERS}


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


# --- контракт маршрутов (001.13) ---------------------------------------------------------------


async def test_seven_routes_in_openapi_with_schemas(app_client: httpx.AsyncClient) -> None:
    schema = (await app_client.get("/openapi.json")).json()
    for path, model in AUTH_ROUTES.items():
        operation = schema["paths"][path]["post"]
        if model is None:
            assert "requestBody" not in operation, path
            assert "204" in operation["responses"], path
        else:
            ref = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
            assert ref == f"#/components/schemas/{model}", path
            assert model in schema["components"]["schemas"]
    assert {p for p in schema["paths"] if p.startswith("/api/v1/auth/")} == set(AUTH_ROUTES), (
        "ровно семь маршрутов раздела"
    )
    status_values = schema["components"]["schemas"]["StatusOut"]["properties"]["status"]
    assert set(status_values["enum"]) == {
        "unconfirmed",
        "confirmed",
        "ok",
        "accepted",
        "password_changed",
    }


@pytest.mark.parametrize(
    ("path", "body", "loc"),
    [
        ("/api/v1/auth/register", {**VALID_REGISTER, "email": "not-an-email"}, ["body", "email"]),
        ("/api/v1/auth/register", {**VALID_REGISTER, "password": "short"}, ["body", "password"]),
        ("/api/v1/auth/register", {**VALID_REGISTER, "lang": "de"}, ["body", "lang"]),
        (
            "/api/v1/auth/register",
            {"email": "a@b.io", "password": "x" * 8},
            ["body", "aup_version"],
        ),
        ("/api/v1/auth/register", {**VALID_REGISTER, "is_admin": True}, ["body", "is_admin"]),
        ("/api/v1/auth/login", {"email": "a@b.io"}, ["body", "password"]),
        (
            "/api/v1/auth/login",
            {"email": "a@b.io", "password": "x" * 8, "junk": 1},
            ["body", "junk"],
        ),
        ("/api/v1/auth/verify", {"token": "short"}, ["body", "token"]),
        ("/api/v1/auth/verify", {"token": "t" * 32, "is_admin": True}, ["body", "is_admin"]),
        ("/api/v1/auth/reset-request", {"email": "ad\x00min@b.io"}, ["body", "email"]),
        ("/api/v1/auth/reset-request", {"email": "a@b.io\r\nBcc: x@y.z"}, ["body", "email"]),
        ("/api/v1/auth/reset-request", {"email": "a@b.io", "extra": 1}, ["body", "extra"]),
        (
            "/api/v1/auth/reset-confirm",
            {"token": "t" * 32, "password": "short"},
            ["body", "password"],
        ),
        (
            "/api/v1/auth/reset-confirm",
            {"token": "t" * 32, "password": "x" * 8, "role": "root"},
            ["body", "role"],
        ),
    ],
)
async def test_validation_errors_use_unified_format(
    app_client: httpx.AsyncClient, path: str, body: dict[str, object], loc: list[str]
) -> None:
    response = await app_client.post(path, json=body)
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert loc in [e["loc"] for e in error["details"]["errors"]], error
    assert "set-cookie" not in response.headers


# --- UC-02 ---------------------------------------------------------------------------------------


async def test_uc02_full_registration_cycle(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-01: регистрация → письмо из очереди → подтверждение → вход."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("alice")
        async with stand.client() as client:
            register = await client.post(
                "/api/v1/auth/register",
                json={"email": address.upper(), "password": PASSWORD, "aup_version": "2026-09"},
            )
            assert register.status_code == 201 and register.json() == {"status": "unconfirmed"}
            user = await stand.user(address)
            assert user is not None and user["email"] == address, "хранится нормализованным (§16.8)"
            assert user["email_verified_at"] is None and user["status"] == "active"
            assert user["aup_version"] == "2026-09" and user["aup_accepted_at"] is not None
            assert user["language"] == "en"

            unconfirmed = await login(client, address)
            assert unconfirmed.status_code == 401, "вход до подтверждения адреса закрыт"
            assert unconfirmed.json()["error"]["code"] == "invalid_credentials"

            token = await stand.mail_token(address, "verify")
            assert len(token) >= 32
            assert await stand.mail_count(address, "verify") == 1
            verified = await client.post("/api/v1/auth/verify", json={"token": token})
            assert verified.status_code == 200 and verified.json() == {"status": "confirmed"}
            user = await stand.user(address)
            assert user is not None and user["email_verified_at"] is not None
            reused = await client.post("/api/v1/auth/verify", json={"token": token})
            assert reused.status_code == 410, "ссылка одноразовая (AC-31)"

            ok = await login(client, address)
            assert ok.status_code == 200 and ok.json() == {"status": "ok"}
            cookies = cookies_of(ok)
            sid, attributes = cookies["sid"]
            assert attributes["httponly"] == "true" and attributes["secure"] == "true"
            assert attributes["samesite"] == "lax" and attributes["path"] == "/"
            csrf, csrf_attributes = cookies["csrf"]
            assert "httponly" not in csrf_attributes and csrf_attributes["secure"] == "true"
            session = await SessionStore(stand.redis).get(sid)
            assert session is not None and session.subject_id == str(user["id"])
            assert session.kind == "user" and session.ip.startswith("203.0.113.")
            assert session.csrf == csrf

            wrong = await login(client, address, "wrong password!")
            assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
        assert await stand.auth_events(user["id"]) == [
            ("register", "success"),
            ("login", "denied"),
            ("login", "success"),
            ("login", "denied"),
        ]


async def test_uc02_a1_duplicate_email_is_rejected(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-02: повторная регистрация того же нормализованного адреса (другой регистр,
    пробелы) → 409 ``email_taken``; письмо повторно не ставится."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("dup")
        async with stand.client() as client:
            first = await client.post(
                "/api/v1/auth/register",
                json={"email": address, "password": PASSWORD, "aup_version": "2026-09"},
            )
            assert first.status_code == 201
            second = await client.post(
                "/api/v1/auth/register",
                json={"email": f" {address.upper()} ", "password": PASSWORD, "aup_version": "1"},
            )
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "email_taken"
        assert await stand.mail_count(address, "verify") == 1


async def test_uc02_a5_login_rate_limit_by_ip_does_not_block_account(
    pg_dsn: str, redis_url: str
) -> None:
    """TC-E2E-03: шесть неверных паролей с одного адреса — шестой запрос 429; с другого адреса
    верный пароль входит (счётчик по учётной записи не блокирует учётную запись)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("bruteforce")
        await stand.register_verified(address)
        async with stand.client() as attacker:
            statuses = [
                (await login(attacker, address, f"wrong-{i:02d}")).status_code for i in range(6)
            ]
            assert statuses == [401] * 5 + [429], statuses
            sixth = await login(attacker, address, "wrong-05")
            assert sixth.json()["error"]["code"] == "rate_limited"
            assert 1 <= int(sixth.headers["retry-after"]) <= 60, "отказ с задержкой (§5.12)"
        async with stand.client() as owner:
            assert (await login(owner, address)).status_code == 200, (
                "учётная запись не заблокирована"
            )


async def ten_failures_from_five_addresses(stand: AuthStand, victim: str) -> None:
    """Десять неудач по учётной записи с пяти адресов источника — порог адреса не задет."""
    for ip_octet in range(101, 106):
        async with stand.client(f"203.0.113.{ip_octet}") as client:
            for i in range(2):
                assert (await login(client, victim, f"bad-{ip_octet}-{i}")).status_code == 401


class SolvedCaptcha:
    """Провайдер CAPTCHA для теста: верен только ответ ``solved`` (ОВ-A3 — боевого нет)."""

    async def verify(self, token: str | None, ip: str | None) -> bool:
        return token == "solved"  # noqa: S105 — ответ CAPTCHA теста, не пароль


class SingleUseCaptcha:
    """Модель настоящего провайдера: каждый ответ ``fresh-…`` принимается ровно один раз,
    повторная проверка — «уже использован»; считает обращения."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.calls = 0

    async def verify(self, token: str | None, ip: str | None) -> bool:
        self.calls += 1
        if token is None or not token.startswith("fresh-") or token in self.used:
            return False
        self.used.add(token)
        return True


async def test_uc02_a5_account_threshold_never_locks_owner_without_provider(
    pg_dsn: str, redis_url: str
) -> None:
    """§5.12: «блокировка учётной записи не применяется». Счётчик по учётной записи хранит только
    отказы; без провайдера CAPTCHA (ОВ-A3) верные учётные данные проходят всегда — чужие десять
    неудач не запирают владельца, успешный вход обнуляет счётчик (ревью S-1)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        victim = stand.email("victim")
        other = stand.email("other")
        await stand.register_verified(victim)
        await stand.register_verified(other)
        await ten_failures_from_five_addresses(stand, victim)
        account_key = f"rl:login:account:{victim}"
        assert await stand.redis.zcard(account_key) == 10, "десять отказов засчитаны"
        async with stand.client("203.0.113.110") as owner:
            assert (await login(owner, victim)).status_code == 200, "владелец не заперт"
            assert await stand.redis.exists(account_key) == 0, "успех обнуляет счётчик"
            assert (await login(owner, victim, "still wrong")).status_code == 401
            assert await stand.redis.zcard(account_key) == 1, "отказ засчитан заново"
            assert (await login(owner, other)).status_code == 200, "другая учётная запись входит"
        async with stand.client("203.0.113.111") as owner:
            for _ in range(3):  # успешные входы порог не тратят
                assert (await login(owner, victim)).status_code == 200
            assert await stand.redis.exists(account_key) == 0


async def test_uc02_a5_account_threshold_requires_captcha_with_provider(
    pg_dsn: str, redis_url: str
) -> None:
    """§5.12 с настроенным провайдером: над порогом CAPTCHA проверяется до пароля — без ответа
    429 ``captcha_required`` даже с верным паролем, с верным ответом — вход и сброс счётчика;
    неверный пароль с верным ответом — 401 и отказ засчитан."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        victim = stand.email("victim")
        await stand.register_verified(victim)
        stand.app.dependency_overrides[get_user_service] = lambda: UserService(
            stand.pool, captcha=SolvedCaptcha()
        )
        await ten_failures_from_five_addresses(stand, victim)
        account_key = f"rl:login:account:{victim}"
        async with stand.client("203.0.113.110") as owner:
            gated = await login(owner, victim)
            assert gated.status_code == 429, gated.text
            assert gated.json()["error"]["code"] == "captcha_required"
            assert gated.json()["error"]["details"] == {"captcha_required": True}
            assert "set-cookie" not in gated.headers
            wrong_answer = await owner.post(
                "/api/v1/auth/login",
                json={"email": victim, "password": PASSWORD, "captcha_token": "nope"},
            )
            assert wrong_answer.status_code == 429
            solved = await owner.post(
                "/api/v1/auth/login",
                json={"email": victim, "password": PASSWORD, "captcha_token": "solved"},
            )
            assert solved.status_code == 200, solved.text
            assert await stand.redis.exists(account_key) == 0, "успех обнуляет счётчик"
            guess = await owner.post(
                "/api/v1/auth/login",
                json={"email": victim, "password": "guess-guess-guess", "captcha_token": "solved"},
            )
            assert guess.status_code == 401, "верный ответ CAPTCHA не заменяет пароль"
            assert await stand.redis.zcard(account_key) == 1


async def test_uc02_a5_registration_threshold_offers_captcha_with_provider(
    pg_dsn: str, redis_url: str
) -> None:
    """§5.12: реакция порога регистрации — CAPTCHA. С настроенным провайдером шестая регистрация
    с одного адреса источника за час без ответа → 429 ``captcha_required``, с верным ответом →
    201; без провайдера — 429 ``rate_limited`` с ``Retry-After`` (ревью раунда 2, C-1)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        stand.app.dependency_overrides[get_user_service] = lambda: UserService(
            stand.pool, captcha=SolvedCaptcha()
        )
        async with stand.client() as client:
            for i in range(5):
                body = {"email": stand.email(f"r{i}"), "password": PASSWORD, "aup_version": "1"}
                assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
            body = {"email": stand.email("sixth"), "password": PASSWORD, "aup_version": "1"}
            gated = await client.post("/api/v1/auth/register", json=body)
            assert gated.status_code == 429, gated.text
            assert gated.json()["error"]["code"] == "captcha_required"
            assert gated.json()["error"]["details"] == {"captcha_required": True}
            assert await stand.user(body["email"]) is None, "без ответа CAPTCHA записи нет"
            wrong = await client.post(
                "/api/v1/auth/register", json={**body, "captcha_token": "nope"}
            )
            assert wrong.status_code == 429
            solved = await client.post(
                "/api/v1/auth/register", json={**body, "captcha_token": "solved"}
            )
            assert solved.status_code == 201, solved.text
        stand.app.dependency_overrides.clear()
        async with stand.client() as client:  # без провайдера — отказ с задержкой
            for i in range(5):
                body = {"email": stand.email(f"n{i}"), "password": PASSWORD, "aup_version": "1"}
                assert (await client.post("/api/v1/auth/register", json=body)).status_code == 201
            body = {"email": stand.email("plain"), "password": PASSWORD, "aup_version": "1"}
            refused = await client.post(
                "/api/v1/auth/register", json={**body, "captcha_token": "solved"}
            )
            assert refused.status_code == 429
            assert refused.json()["error"]["code"] == "rate_limited"
            assert 1 <= int(refused.headers["retry-after"]) <= 3600


async def test_uc02_a5_login_ip_threshold_offers_captcha_with_provider(
    pg_dsn: str, redis_url: str
) -> None:
    """§5.12: порог входа по адресу источника — тоже CAPTCHA (общий адрес трансляции): с
    провайдером шестая попытка без ответа → 429 ``captcha_required``, с верным ответом идёт к
    проверке пароля — неверный 401, верный 200 (ревью раунда 2, L-1)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("nat")
        await stand.register_verified(address)
        stand.app.dependency_overrides[get_user_service] = lambda: UserService(
            stand.pool, captcha=SolvedCaptcha()
        )
        async with stand.client() as shared_nat:
            for i in range(5):
                assert (await login(shared_nat, address, f"wrong-{i:02d}")).status_code == 401
            gated = await login(shared_nat, address)
            assert gated.status_code == 429, gated.text
            assert gated.json()["error"]["code"] == "captcha_required"
            assert "set-cookie" not in gated.headers
            guess = await shared_nat.post(
                "/api/v1/auth/login",
                json={"email": address, "password": "wrong-again", "captcha_token": "solved"},
            )
            assert guess.status_code == 401, "верный ответ CAPTCHA не заменяет пароль"
            human = await shared_nat.post(
                "/api/v1/auth/login",
                json={"email": address, "password": PASSWORD, "captcha_token": "solved"},
            )
            assert human.status_code == 200, human.text
            assert "sid" in cookies_of(human)


async def test_one_captcha_answer_serves_both_login_thresholds(pg_dsn: str, redis_url: str) -> None:
    """Ответ CAPTCHA одноразовый у настоящих провайдеров: когда переступлены оба порога входа
    (адрес источника и учётная запись), один ответ проверяется у провайдера ровно один раз —
    иначе вторая проверка получала бы «уже использован» и владелец за общим адресом был бы
    заперт (ревью раунда 3, S-1)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("nat2")
        await stand.register_verified(address)
        provider = SingleUseCaptcha()
        stand.app.dependency_overrides[get_user_service] = lambda: UserService(
            stand.pool, captcha=provider
        )
        async with stand.client() as shared_nat:
            for i in range(5):  # порог адреса источника; те же отказы — половина порога записи
                assert (await login(shared_nat, address, f"wrong-{i:02d}")).status_code == 401
            for ip_octet in range(121, 126):  # ещё пять отказов с других адресов — порог записи
                async with stand.client(f"203.0.113.{ip_octet}") as other:
                    assert (await login(other, address, f"bad-pass-{ip_octet}")).status_code == 401
            assert await stand.redis.zcard(f"rl:login:account:{address}") == 10
            gated = await login(shared_nat, address)
            assert gated.status_code == 429 and gated.json()["error"]["code"] == "captcha_required"
            calls_before = provider.calls
            human = await shared_nat.post(
                "/api/v1/auth/login",
                json={"email": address, "password": PASSWORD, "captcha_token": "fresh-1"},
            )
            assert human.status_code == 200, human.text
            assert provider.calls - calls_before == 1, "провайдер спрошен один раз за запрос"
            reused = await shared_nat.post(
                "/api/v1/auth/login",
                json={"email": address, "password": PASSWORD, "captcha_token": "fresh-1"},
            )
            assert reused.status_code == 429, "использованный ответ не принимается повторно"


async def test_one_captcha_answer_serves_registration_thresholds_and_setting(
    pg_dsn: str, redis_url: str
) -> None:
    """Регистрация над порогами адреса источника и домена при включённой настройке
    ``captcha.enabled``: три места требуют ответ, провайдер спрошен один раз → 201."""
    async with auth_stand(pg_dsn, redis_url, {"captcha": {"enabled": True}}) as stand:
        provider = SingleUseCaptcha()
        stand.app.dependency_overrides[get_user_service] = lambda: UserService(
            stand.pool, captcha=provider
        )
        limiter = ratelimit.RateLimiter(stand.redis)
        domain_key = ratelimit.register_email_domain("test-auth.local")
        for _ in range(ratelimit.REGISTER_DOMAIN.count):  # порог домена — отметками, не argon2
            await limiter.check(domain_key, ratelimit.REGISTER_DOMAIN.count, 3600)
        async with stand.client() as client:
            for i in range(5):  # порог адреса источника (записи не создаются: домен над порогом)
                body = {"email": stand.email(f"d{i}"), "password": PASSWORD, "aup_version": "1"}
                assert (await client.post("/api/v1/auth/register", json=body)).status_code == 429
            body = {"email": stand.email("fresh"), "password": PASSWORD, "aup_version": "1"}
            calls_before = provider.calls
            created = await client.post(
                "/api/v1/auth/register", json={**body, "captcha_token": "fresh-2"}
            )
            assert created.status_code == 201, created.text
            assert provider.calls - calls_before == 1, "провайдер спрошен один раз за запрос"
            assert await stand.user(body["email"]) is not None


async def test_blocked_or_deleted_account_cannot_login_or_reset(
    pg_dsn: str, redis_url: str
) -> None:
    """Блокировка и удаление закрывают вход верным паролем (401, причина не раскрывается,
    отказ в ``auth_events``) и выдачу ссылки восстановления (ревью C-1)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("blocked")
        user_id = await stand.register_verified(address)
        async with stand.pool.acquire() as conn:
            await conn.execute("update users set status = 'blocked' where id = $1", user_id)
        async with stand.client() as client:
            denied = await login(client, address)
            assert denied.status_code == 401, "заблокированная запись не входит"
            assert denied.json()["error"]["code"] == "invalid_credentials"
            assert "set-cookie" not in denied.headers
            assert (
                await client.post("/api/v1/auth/reset-request", json={"email": address})
            ).status_code == 202
            assert await stand.mail_count(address, "reset") == 0, "ссылка не выдаётся"
            assert await stand.mail_count(address, "reset_unknown") == 1
        assert (await stand.auth_events(user_id))[-1] == ("login", "denied")
        async with stand.pool.acquire() as conn:
            await conn.execute(
                "update users set status = 'active', deleted_at = now() where id = $1", user_id
            )
        async with stand.client() as client:
            assert (await login(client, address)).status_code == 401, "удалённая запись не входит"


async def test_concurrent_registrations_of_one_address(pg_dsn: str, redis_url: str) -> None:
    """Две одновременные регистрации одного адреса (двойной клик): 201 и 409 ``email_taken``,
    одна запись и одно письмо — уникальность решает индекс, а не «проверить, потом вставить»
    (ревью S-2)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("race")
        body = {"email": address, "password": PASSWORD, "aup_version": "2026-09"}
        async with stand.client() as first, stand.client() as second:
            responses = await asyncio.gather(
                first.post("/api/v1/auth/register", json=body),
                second.post("/api/v1/auth/register", json=body),
            )
        statuses = sorted(r.status_code for r in responses)
        assert statuses == [201, 409], [r.text for r in responses]
        codes = {r.json().get("error", {}).get("code") for r in responses}
        assert "email_taken" in codes
        async with stand.pool.acquire() as conn:
            assert await conn.fetchval("select count(*) from users where email = $1", address) == 1
        assert await stand.mail_count(address, "verify") == 1


async def test_uc02_a6_a7_registration_modes(pg_dsn: str, redis_url: str) -> None:
    async with auth_stand(pg_dsn, redis_url, {"registration_mode": "closed"}) as stand:
        async with stand.client() as client:
            closed = await client.post(
                "/api/v1/auth/register",
                json={"email": stand.email("closed"), "password": PASSWORD, "aup_version": "1"},
            )
            assert closed.status_code == 403
            assert closed.json()["error"]["code"] == "registration_closed"
            await stand.set_setting("registration_mode", "invite")
            invite = await client.post(
                "/api/v1/auth/register",
                json={"email": stand.email("invite"), "password": PASSWORD, "aup_version": "1"},
            )
            assert invite.status_code == 403
            assert invite.json()["error"]["code"] == "invite_required"
            await stand.set_setting("registration_mode", "open")
            opened = await client.post(
                "/api/v1/auth/register",
                json={"email": stand.email("open"), "password": PASSWORD, "aup_version": "1"},
            )
            assert opened.status_code == 201


async def test_disposable_domain_and_captcha_settings(pg_dsn: str, redis_url: str) -> None:
    """§16.8: одноразовый домен → 400; §4.1: включённая CAPTCHA без провайдера → 400
    (fail-closed)."""
    overrides = {"disposable_email_domains": ["Mailinator.com", "10minutemail.net"]}
    async with auth_stand(pg_dsn, redis_url, overrides) as stand:
        stand.emails.add("someone@mailinator.com")  # уборка счётчика домена mailinator.com
        async with stand.client() as client:
            disposable = await client.post(
                "/api/v1/auth/register",
                json={"email": "someone@MAILINATOR.com", "password": PASSWORD, "aup_version": "1"},
            )
            assert disposable.status_code == 400
            assert disposable.json()["error"]["code"] == "disposable_email"
            await stand.set_setting("captcha", {"enabled": True})
            captcha = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": stand.email("captcha"),
                    "password": PASSWORD,
                    "aup_version": "1",
                    "captcha_token": "anything",
                },
            )
            assert captcha.status_code == 400
            assert captcha.json()["error"]["code"] == "captcha_required"


# --- UC-15 ---------------------------------------------------------------------------------------


async def test_uc15_reset_invalidates_sessions_and_link_is_single_use(
    pg_dsn: str, redis_url: str
) -> None:
    """TC-E2E-04: две активные сессии → смена пароля по ссылке → обе недействительны, повторный
    переход по ссылке — 410 (AC-31); старый пароль не входит, новый входит; событие ``reset``."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("reset")
        user_id = await stand.register_verified(address)
        store = SessionStore(stand.redis)
        sids = []
        for octet in (11, 12):
            async with stand.client(f"203.0.113.{octet}") as client:
                response = await login(client, address)
                assert response.status_code == 200
                sids.append(cookies_of(response)["sid"][0])
        assert all([await store.get(sid) for sid in sids])

        async with stand.client() as client:
            requested = await client.post("/api/v1/auth/reset-request", json={"email": address})
            assert requested.status_code == 202
            token = await stand.mail_token(address, "reset")
            assert all([await store.get(sid) for sid in sids]), "запрос ссылки сессии не трогает"
            confirmed = await client.post(
                "/api/v1/auth/reset-confirm",
                json={"token": token, "password": "brand new password 42"},
            )
            assert confirmed.status_code == 200
            assert confirmed.json() == {"status": "password_changed"}
            assert [await store.get(sid) for sid in sids] == [None, None], "сессии отозваны"
            reused = await client.post(
                "/api/v1/auth/reset-confirm", json={"token": token, "password": "another one 43"}
            )
            assert reused.status_code == 410
            assert reused.json()["error"]["code"] == "token_expired"
            assert (await login(client, address, PASSWORD)).status_code == 401
            assert (await login(client, address, "brand new password 42")).status_code == 200
        events = await stand.auth_events(user_id)
        assert ("reset", "success") in events


async def test_uc15_a1_unknown_address_is_indistinguishable(pg_dsn: str, redis_url: str) -> None:
    """UC-15 A1: ответ на несуществующий адрес — тот же статус, байты тела, заголовки и тот же
    путь кода (задача в очереди ставится в обоих случаях); время ответа сопоставимо."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        known = stand.email("known")
        await stand.register_verified(known)
        unknown = stand.email("unknown")
        timings: dict[str, list[float]] = {known: [], unknown: []}
        responses: dict[str, httpx.Response] = {}
        # Порог по адресу почты — 3 в час: каждая выборка идёт на свой адрес того же вида.
        addresses = {known: [known], unknown: [unknown]}
        for i in range(1, ROUNDS // 3 + 1):
            extra = stand.email(f"known{i}")
            await stand.register_verified(extra)
            addresses[known].append(extra)
            addresses[unknown].append(stand.email(f"unknown{i}"))
        for i in range(ROUNDS):
            for label in (known, unknown):
                address = addresses[label][i // 3]
                async with stand.client() as client:  # свой адрес источника — без порогов
                    started = time.perf_counter()
                    responses[label] = await client.post(
                        "/api/v1/auth/reset-request", json={"email": address}
                    )
                    timings[label].append(time.perf_counter() - started)
        first, second = responses[known], responses[unknown]
        assert first.status_code == second.status_code == 202
        assert first.content == second.content == b'{"status":"accepted"}'
        assert stable_headers(first) == stable_headers(second)
        assert await stand.mail_count(known, "reset") == 3
        assert await stand.mail_count(unknown, "reset_unknown") == 3, "тот же путь: задача есть"
        assert await stand.mail_count(unknown, "reset") == 0
        known_ms = statistics.median(timings[known]) * 1000
        unknown_ms = statistics.median(timings[unknown]) * 1000
        # Один набор запросов для обоих случаев: расхождение медиан — только физическая запись
        # строки токена, много меньше самого запроса. Порог относительный и абсолютный (ревью
        # C-2): задержка в десятки миллисекунд на одной из ветвей — красный тест.
        assert abs(known_ms - unknown_ms) < max(TIMING_FLOOR_MS, known_ms * TIMING_RATIO), (
            f"известный {known_ms:.2f} мс, неизвестный {unknown_ms:.2f} мс"
        )


async def test_authenticate_costs_the_same_for_unknown_address(pg_dsn: str, redis_url: str) -> None:
    """Неизвестный адрес проверяется с хешем-пустышкой: время ответа не выдаёт существование
    учётной записи (argon2id §7.2 стоит десятки миллисекунд, запрос к базе — единицы)."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        known = stand.email("timing")
        await stand.register_verified(known)
        service = UserService(stand.pool)
        timings: dict[str, list[float]] = {"known": [], "unknown": []}
        for _ in range(5):
            for label, address in (("known", known), ("unknown", stand.email("ghost"))):
                started = time.perf_counter()
                assert await service.authenticate(address, "wrong password!") is None
                timings[label].append(time.perf_counter() - started)
        known_ms = statistics.median(timings["known"]) * 1000
        unknown_ms = statistics.median(timings["unknown"]) * 1000
        assert known_ms > 5, f"argon2id должен стоить заметно: {known_ms:.1f} мс"
        assert abs(known_ms - unknown_ms) < known_ms / 2, (
            f"известный {known_ms:.1f} мс, неизвестный {unknown_ms:.1f} мс"
        )


async def test_uc15_a2_expired_and_foreign_links(pg_dsn: str, redis_url: str) -> None:
    """Просроченная ссылка и ссылка неверного вида → 410; чужой (неизвестный) токен → 410."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("expired")
        user_id = await stand.register_verified(address)
        async with stand.pool.acquire() as conn:
            await conn.execute(
                "insert into email_tokens (user_id, kind, token_hash, expires_at) "
                "values ($1, 'reset', $2, now() - interval '1 second')",
                user_id,
                hash_token("expired-token-" + "x" * 20),
            )
        async with stand.client() as client:
            expired = await client.post(
                "/api/v1/auth/reset-confirm",
                json={"token": "expired-token-" + "x" * 20, "password": "new password 12345"},
            )
            assert expired.status_code == 410
            unknown = await client.post(
                "/api/v1/auth/reset-confirm",
                json={"token": "n" * 32, "password": "new password 12345"},
            )
            assert unknown.status_code == 410
            verify_token = await stand.mail_token(address, "verify")  # уже использован
            wrong_kind = await client.post(
                "/api/v1/auth/reset-confirm",
                json={"token": verify_token, "password": "new password 12345"},
            )
            assert wrong_kind.status_code == 410, "токен подтверждения не годится для сброса"
            assert (await login(client, address)).status_code == 200, "пароль не изменился"


async def test_uc15_a3_reset_request_rate_limit(pg_dsn: str, redis_url: str) -> None:
    """Порог по адресу почты (3 за час): четвёртый запрос → 429; другой адрес не задет."""
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("flood")
        async with stand.client() as client:
            statuses = [
                (
                    await client.post("/api/v1/auth/reset-request", json={"email": address})
                ).status_code
                for _ in range(4)
            ]
            assert statuses == [202, 202, 202, 429]
            refused = await client.post("/api/v1/auth/reset-request", json={"email": address})
            assert refused.json()["error"]["code"] == "rate_limited"
            assert 1 <= int(refused.headers["retry-after"]) <= 3600, "отказ с задержкой (§5.12)"
            other = await client.post(
                "/api/v1/auth/reset-request", json={"email": stand.email("o")}
            )
            assert other.status_code == 202


# --- выход и CSRF (§7.3) ------------------------------------------------------------------------


async def test_logout_requires_csrf_and_revokes_sessions(pg_dsn: str, redis_url: str) -> None:
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("logout")
        await stand.register_verified(address)
        store = SessionStore(stand.redis)
        async with stand.client() as client:
            first = await login(client, address)
            sid, _ = cookies_of(first)["sid"]
            csrf, _ = cookies_of(first)["csrf"]
            client.cookies.clear()
            second = await login(client, address)
            other_sid, _ = cookies_of(second)["sid"]
            client.cookies.clear()

            client.cookies.set("sid", sid)
            missing = await client.post("/api/v1/auth/logout")
            assert missing.status_code == 403 and missing.json()["error"]["code"] == "csrf_failed"
            forged = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "x" * 43})
            assert forged.status_code == 403
            assert await store.get(sid) is not None, "сессия цела после отказов CSRF"

            logout = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
            assert logout.status_code == 204
            cleared = cookies_of(logout)
            assert cleared["sid"][1].get("max-age") == "0"
            assert cleared["csrf"][1].get("max-age") == "0"
            assert await store.get(sid) is None and await store.get(other_sid) is not None

            client.cookies.clear()
            anonymous = await client.post("/api/v1/auth/logout")
            assert anonymous.status_code == 204, "без сессии выход идемпотентен"

            third = await login(client, address)
            third_sid, _ = cookies_of(third)["sid"]
            third_csrf, _ = cookies_of(third)["csrf"]
            client.cookies.clear()
            client.cookies.set("sid", third_sid)
            everywhere = await client.post(
                "/api/v1/auth/logout-all", headers={"X-CSRF-Token": third_csrf}
            )
            assert everywhere.status_code == 204
            assert await store.get(other_sid) is None and await store.get(third_sid) is None


async def test_login_rejects_when_authenticate_returns_none(
    pg_dsn: str, redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контракт маршрута: ``authenticate → None`` — 401 ``invalid_credentials`` и никакой
    cookie (проверяется подменой, независимо от базы). Клиент — со своим адресом источника:
    счётчик входа по адресу живёт минуту, и общий адрес ``app_client`` копит попытки между
    прогонами (пять прогонов за минуту давали 429 вместо 401)."""

    async def deny(self: UserService, email: str, password: str, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(UserService, "authenticate", deny)
    async with auth_stand(pg_dsn, redis_url) as stand, stand.client() as client:
        denied = await client.post(
            "/api/v1/auth/login", json={"email": stand.email("deny"), "password": "wrong password"}
        )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in denied.headers


async def test_session_cookie_ttl_matches_declared(pg_dsn: str, redis_url: str) -> None:
    async with auth_stand(pg_dsn, redis_url) as stand:
        address = stand.email("ttl")
        await stand.register_verified(address)
        async with stand.client() as client:
            response = await login(client, address)
            sid, attributes = cookies_of(response)["sid"]
            assert int(attributes["max-age"]) == 30 * 24 * 3600
            ttl = await stand.redis.ttl(f"sess:{sid}")
            assert 30 * 24 * 3600 - 60 < ttl <= 30 * 24 * 3600
            expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)
            assert expires_at > dt.datetime.now(dt.UTC)
