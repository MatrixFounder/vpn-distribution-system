"""Ограничение частоты (security.md §7.3, постановка §5.12): счётчики в Redis с ключами по
защищаемой операции. Отказ Redis — fail-closed (§9.1): ``RateLimitUnavailable`` → 503 с
``Retry-After``. Задача 001.12 — интерфейс и ключи; сам счётчик — 001.14
(``NotImplementedError``)."""

from __future__ import annotations

import redis.asyncio as redis_async
from redis.exceptions import RedisError

RETRY_AFTER_SECONDS = 5


class RateLimitUnavailable(Exception):  # noqa: N818 — имя из контракта задачи 001.12
    """Redis недоступен: операции с лимитом частоты не выполняются (fail-closed, §9.1)."""


def key(operation: str, dimension: str, value: str) -> str:
    """Ключ счётчика ``rl:<операция>:<измерение>:<значение>`` — измерения по §5.12."""
    return f"rl:{operation}:{dimension}:{value}"


# Ключи §5.12: вход — адрес и учётная запись раздельно; регистрация — адрес и домен почты;
# восстановление и повторное письмо — адрес почты; подписка — токен, для несуществующего —
# адрес источника с отдельным порогом; коды — учётная запись и адрес; перевыпуск токена и
# публичный API — учётная запись; Node API — identity ноды.
def login_ip(ip: str) -> str:
    return key("login", "ip", ip)


def login_account(account: str) -> str:
    return key("login", "account", account)


def register_ip(ip: str) -> str:
    return key("register", "ip", ip)


def register_email_domain(domain: str) -> str:
    return key("register", "domain", domain.lower())


def reset_email(email: str) -> str:
    return key("reset", "email", email.lower())


def resend_email(email: str) -> str:
    return key("resend", "email", email.lower())


def subscription_token(token_hash: str) -> str:
    return key("subscription", "token", token_hash)


def subscription_unknown_ip(ip: str) -> str:
    return key("subscription-unknown", "ip", ip)


def redeem_account(account: str) -> str:
    return key("redeem", "account", account)


def redeem_ip(ip: str) -> str:
    return key("redeem", "ip", ip)


def token_reissue_account(account: str) -> str:
    return key("token-reissue", "account", account)


def api_account(account: str) -> str:
    return key("api", "account", account)


def node_identity(identity: str) -> str:
    return key("node", "identity", identity)


def enrollment(ip: str, token_hash: str) -> str:
    return key("enroll", "ip-token", f"{ip}:{token_hash}")


class RateLimiter:
    """Счётчики частоты поверх клиента Redis."""

    def __init__(self, client: redis_async.Redis) -> None:
        self._redis = client

    async def ensure_available(self) -> None:
        """Проверить доступность Redis; недоступен → ``RateLimitUnavailable`` (fail-closed)."""
        try:
            await self._redis.ping()
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(f"Redis недоступен: {type(exc).__name__}: {exc}") from exc

    async def check(self, counter_key: str, limit: int, window_s: int) -> bool:
        """Истина, если запрос в пределах ``limit`` за окно ``window_s`` — 001.14
        (``counter_key`` — ключ из функций этого модуля). Недоступный Redis даёт
        ``RateLimitUnavailable`` и здесь."""
        await self.ensure_available()
        raise NotImplementedError("RateLimiter.check — задача 001.14")
