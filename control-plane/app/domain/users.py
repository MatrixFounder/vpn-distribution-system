"""Доменный сервис учётных записей пользователей (UC-02, UC-15; постановка §4.1, §5.12, §16.8;
security.md §7.1). Задача 001.14: настоящая логика поверх пула asyncpg и очереди задач.

Адрес нормализуется (нижний регистр, обрезка пробелов) и хранится в ``users.email`` (citext);
одноразовые домены — список в ``settings.disposable_email_domains``; режим регистрации —
``settings.registration_mode`` (``open`` | ``invite`` | ``closed``); CAPTCHA — ``settings.captcha``
и внешний ``CaptchaVerifier`` (провайдер — открытый вопрос ОВ-A3: по умолчанию включённая CAPTCHA
без проверяющего отклоняет запрос, fail-closed). Ссылки подтверждения и восстановления —
одноразовые токены ``email_tokens`` (хранится только SHA-256, срок ОВ-25: подтверждение 24 ч,
восстановление 1 ч — умолчания до решения); письмо ставится в очередь ``background`` задачей
``send_email`` (обработчик — 001.52), полезная нагрузка содержит токен, который получатель и
должен получить. Все ошибки — ``ApiError`` единого формата.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from typing import Any, Literal, Protocol

import asyncpg

from app.db.pool import transaction
from app.errors import ApiError
from app.jobs import queue as jobs
from app.security.passwords import hash_password, verify_password

UserId = uuid.UUID
RegistrationMode = Literal["open", "invite", "closed"]

VERIFY_TOKEN_TTL = dt.timedelta(hours=24)  # ОВ-25, умолчание
RESET_TOKEN_TTL = dt.timedelta(hours=1)  # ОВ-25, умолчание
EMAIL_QUEUE = "background"
EMAIL_JOB_TYPE = "send_email"

# Хеш пароля-пустышки: проверка неизвестного адреса стоит столько же, сколько известного
# (аутентификация не выдаёт существование адреса временем ответа).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


class CaptchaVerifier(Protocol):
    """Проверка ответа CAPTCHA у провайдера (ОВ-A3)."""

    async def verify(self, token: str | None, ip: str | None) -> bool: ...


class NoCaptchaProvider:
    """Провайдер не настроен: при включённой CAPTCHA любой ответ отклоняется (fail-closed)."""

    async def verify(self, token: str | None, ip: str | None) -> bool:
        return False


class CaptchaAnswer:
    """Ответ CAPTCHA одного запроса. Провайдер спрашивается не более одного раза: ответы
    одноразовые (повторная проверка того же ответа у reCAPTCHA/hCaptcha/Turnstile — «уже
    использован»), а порогов, требующих ответа, в одном запросе может быть несколько — адрес
    источника и учётная запись при входе, адрес источника и домен при регистрации плюс
    настройка ``captcha.enabled`` (ревью 001.14, раунд 3)."""

    def __init__(self, verifier: CaptchaVerifier, token: str | None, ip: str | None) -> None:
        self._verifier = verifier
        self._token = token
        self._ip = ip
        self._verified: bool | None = None
        self.available = not isinstance(verifier, NoCaptchaProvider)

    async def verified(self) -> bool:
        """Истина, если ответ подтверждён провайдером; результат первого обращения запоминается."""
        if self._verified is None:
            self._verified = self._token is not None and await self._verifier.verify(
                self._token, self._ip
            )
        return self._verified


def normalize_email(email: str) -> str:
    """Нормализация §16.8: обрезка пробелов, нижний регистр всего адреса (домен и локальная
    часть — почтовые службы, где регистр локальной части значим, практически отсутствуют)."""
    return email.strip().lower()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class UserService:
    """Регистрация, подтверждение адреса, аутентификация, восстановление пароля."""

    def __init__(self, pool: asyncpg.Pool, captcha: CaptchaVerifier | None = None) -> None:
        self._pool = pool
        self._captcha: CaptchaVerifier = captcha or NoCaptchaProvider()

    # --- настройки -------------------------------------------------------------------------

    async def _settings(self, conn: asyncpg.Connection) -> dict[str, Any]:
        rows = await conn.fetch(
            "select key, value from settings "
            "where key in ('registration_mode', 'disposable_email_domains', 'captcha')"
        )
        values = {row["key"]: _json(row["value"]) for row in rows}
        return {
            "registration_mode": values.get("registration_mode", "open"),
            "disposable_email_domains": values.get("disposable_email_domains", []),
            "captcha": values.get("captcha", {"enabled": False}),
        }

    @property
    def captcha_available(self) -> bool:
        """Провайдер CAPTCHA настроен (ОВ-A3). Без него порог входа по учётной записи не может
        требовать CAPTCHA и не блокирует верные учётные данные (§5.12: блокировка учётной записи
        не применяется), а порог регистрации даёт отказ вместо CAPTCHA."""
        return not isinstance(self._captcha, NoCaptchaProvider)

    def captcha_answer(self, token: str | None, ip: str | None) -> CaptchaAnswer:
        """Ответ CAPTCHA запроса с однократной проверкой у провайдера (§5.12; без провайдера —
        ложь, fail-closed, ОВ-A3)."""
        return CaptchaAnswer(self._captcha, token, ip)

    async def _require_captcha_if_enabled(
        self, conn: asyncpg.Connection, answer: CaptchaAnswer
    ) -> None:
        settings = await self._settings(conn)
        if settings["captcha"].get("enabled") and not await answer.verified():
            raise ApiError("captcha_required", "требуется проверка CAPTCHA", status=400)

    # --- регистрация и подтверждение (UC-02 шаги 3–6) ------------------------------------

    async def register(
        self,
        email: str,
        password: str,
        aup_version: str,
        lang: str,
        *,
        captcha_token: str | None = None,
        captcha: CaptchaAnswer | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> UserId:
        """Создать учётную запись «не подтверждена», поставить письмо подтверждения в очередь.
        Ошибки: режим `closed`/`invite` — 403, одноразовый домен — 400, адрес занят — 409.
        ``captcha`` — ответ запроса, уже спрошенный порогами (один вызов провайдера на запрос);
        без него ответ строится из ``captcha_token``."""
        address = normalize_email(email)
        answer = captcha or self.captcha_answer(captcha_token, ip)
        async with transaction(self._pool) as conn:
            settings = await self._settings(conn)
            mode: RegistrationMode = settings["registration_mode"]
            if mode == "closed":
                raise ApiError("registration_closed", "регистрация закрыта", status=403)
            if mode == "invite":  # коды приглашения — отдельная задача; без них форма недоступна
                raise ApiError("invite_required", "регистрация по приглашению", status=403)
            await self._require_captcha_if_enabled(conn, answer)
            domain = address.rsplit("@", 1)[1]
            if domain in {d.lower() for d in settings["disposable_email_domains"]}:
                raise ApiError("disposable_email", "одноразовые адреса не принимаются", status=400)
            # Уникальность решает индекс `users.email`, а не «проверить, потом вставить»: две
            # одновременные регистрации одного адреса дают 201 и 409, а не 500 (ревью S-2).
            # Хеш пароля считается и для занятого адреса (argon2id, десятки миллисекунд) —
            # цена известна: ответ 409 стоит столько же, сколько 201, а частоту регистраций
            # держит порог по адресу источника.
            user_id: UserId | None = await conn.fetchval(
                "insert into users (email, password_hash, language, aup_version, aup_accepted_at) "
                "values ($1, $2, $3, $4, now()) on conflict (email) do nothing returning id",
                address,
                hash_password(password),
                lang,
                aup_version,
            )
            if user_id is None:
                raise ApiError("email_taken", "адрес уже зарегистрирован", status=409)
            await self._issue_token(conn, user_id, "verify", VERIFY_TOKEN_TTL, address, lang)
            await self._auth_event(conn, user_id, "register", ip, user_agent, "success")
        return user_id

    async def verify_email(self, token: str) -> None:
        """Подтвердить адрес по одноразовой ссылке; чужой, использованный или просроченный токен
        — 410 ``token_expired`` (одинаково: факт существования токена не раскрывается)."""
        async with transaction(self._pool) as conn:
            user_id = await self._consume_token(conn, token, "verify")
            await conn.execute(
                "update users set email_verified_at = coalesce(email_verified_at, now()), "
                "updated_at = now() where id = $1",
                user_id,
            )

    # --- вход (UC-02 шаг 6+, §7.1) -----------------------------------------------------------

    async def authenticate(
        self,
        email: str,
        password: str,
        *,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> UserId | None:
        """Идентификатор пользователя по паре адрес/пароль или ``None``; отказ записывается в
        ``auth_events``. Неподтверждённый адрес и удалённая/заблокированная запись — тоже
        ``None`` (не раскрываем причину). Стоимость проверки одинакова для неизвестного адреса."""
        address = normalize_email(email)
        async with transaction(self._pool) as conn:
            row = await conn.fetchrow(
                "select id, password_hash, email_verified_at, status from users "
                "where email = $1 and deleted_at is null",
                address,
            )
            password_ok = verify_password(password, row["password_hash"] if row else _DUMMY_HASH)
            if row is None:
                return None
            allowed = (
                password_ok and row["email_verified_at"] is not None and row["status"] == "active"
            )
            await self._auth_event(
                conn, row["id"], "login", ip, user_agent, "success" if allowed else "denied"
            )
            user_id: UserId = row["id"]
            return user_id if allowed else None

    # --- восстановление пароля (UC-15) ----------------------------------------------------------

    async def request_reset(self, email: str, *, lang: str = "en") -> None:
        """Ссылка восстановления, если адрес зарегистрирован и подтверждён; для любого другого
        адреса — тот же путь кода (задача-пустышка в очереди), чтобы ответ и его время не
        раскрывали существование адреса (UC-15 A1)."""
        address = normalize_email(email)
        token = secrets.token_urlsafe(32)
        async with transaction(self._pool) as conn:
            # Один и тот же набор запросов для любого адреса — без ветвления по существованию
            # записи: строка токена появляется лишь у подтверждённой активной записи (INSERT …
            # SELECT), письмо ставится в очередь в обоих случаях; различие во времени — только
            # физическая запись строки (ревью C-2).
            issued = await conn.fetchrow(
                "with target as (select id, language from users where email = $1 "
                "and deleted_at is null and email_verified_at is not null and status = 'active'), "
                "issued as (insert into email_tokens (user_id, kind, token_hash, expires_at) "
                "select id, 'reset'::email_token_kind, $2, now() + $3 from target "
                "returning id as token_id, user_id) "
                "select issued.token_id, issued.user_id, target.language "
                "from issued join target on target.id = issued.user_id",
                address,
                hash_token(token),
                RESET_TOKEN_TTL,
            )
            payload: dict[str, Any] = {"kind": "reset_unknown", "to": address, "lang": lang}
            idempotency_key = f"email:unknown:{uuid.uuid4()}"
            if issued is not None:
                payload = {
                    "kind": "reset",
                    "to": address,
                    "lang": issued["language"],
                    "token": token,
                    "user_id": str(issued["user_id"]),
                }
                idempotency_key = f"email:{issued['token_id']}"
            await jobs.enqueue(conn, EMAIL_QUEUE, EMAIL_JOB_TYPE, payload, idempotency_key)

    async def confirm_reset(self, token: str, password: str) -> None:
        """Сменить пароль по одноразовой ссылке, аннулировать её; все сессии отзывает
        вызывающий (``SessionStore.revoke_all``) — вернуть идентификатор нельзя из-за сигнатуры
        контракта, поэтому доступен ``confirm_reset_user``."""
        await self.confirm_reset_user(token, password)

    async def confirm_reset_user(self, token: str, password: str) -> UserId:
        """То же, что ``confirm_reset``, но возвращает пользователя для отзыва сессий."""
        async with transaction(self._pool) as conn:
            user_id = await self._consume_token(conn, token, "reset")
            await conn.execute(
                "update users set password_hash = $2, updated_at = now() where id = $1",
                user_id,
                hash_password(password),
            )
            await self._auth_event(conn, user_id, "reset", None, None, "success")
        return user_id

    # --- служебное ------------------------------------------------------------------------------

    async def _issue_token(
        self,
        conn: asyncpg.Connection,
        user_id: UserId,
        kind: Literal["verify", "reset"],
        ttl: dt.timedelta,
        address: str,
        lang: str,
    ) -> None:
        token = secrets.token_urlsafe(32)
        token_id: uuid.UUID = await conn.fetchval(
            "insert into email_tokens (user_id, kind, token_hash, expires_at) "
            "values ($1, $2::email_token_kind, $3, now() + $4) returning id",
            user_id,
            kind,
            hash_token(token),
            ttl,
        )
        await jobs.enqueue(
            conn,
            EMAIL_QUEUE,
            EMAIL_JOB_TYPE,
            {"kind": kind, "to": address, "lang": lang, "token": token, "user_id": str(user_id)},
            f"email:{token_id}",
        )

    async def _consume_token(
        self, conn: asyncpg.Connection, token: str, kind: Literal["verify", "reset"]
    ) -> UserId:
        row = await conn.fetchrow(
            "update email_tokens set used_at = now() where token_hash = $1 and kind = $2 "
            "and used_at is null and expires_at > now() returning user_id",
            hash_token(token),
            kind,
        )
        if row is None:
            raise ApiError("token_expired", "ссылка недействительна или просрочена", status=410)
        user_id: UserId = row["user_id"]
        return user_id

    async def _auth_event(
        self,
        conn: asyncpg.Connection,
        user_id: UserId | None,
        kind: str,
        ip: str | None,
        user_agent: str | None,
        result: str,
    ) -> None:
        await conn.execute(
            "insert into auth_events (user_id, kind, source_ip, user_agent, result) "
            "values ($1, $2::auth_event_kind, $3::inet, $4, $5)",
            user_id,
            kind,
            ip,
            user_agent,
            result,
        )
