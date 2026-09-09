"""Ограничение частоты (security.md §7.3, постановка §5.12): счётчики в Redis с ключами по
защищаемой операции. Отказ Redis — fail-closed (§9.1): ``RateLimitUnavailable`` → 503 с
``Retry-After``. Счётчик — скользящее окно на упорядоченном множестве (001.14): каждая попытка,
включая отклонённую, добавляется с меткой времени, записи старше окна удаляются, решение — по
числу оставшихся; шаги атомарны (Lua), ключ живёт не дольше окна."""

from __future__ import annotations

import time
import uuid

import redis.asyncio as redis_async
from redis.exceptions import RedisError

RETRY_AFTER_SECONDS = 5


class Limit:
    """Порог: не больше ``count`` попыток за ``window_s`` секунд."""

    __slots__ = ("count", "window_s")

    def __init__(self, count: int, window_s: int) -> None:
        self.count, self.window_s = count, window_s


# Пороги §5.12 — константы до настройки в settings.thresholds (ОВ-17): вход по адресу источника
# (6-я неверная попытка за минуту — отказ, TC-E2E-03 001.14), вход по учётной записи (после
# порога — CAPTCHA, блокировки нет), регистрация по адресу и домену почты, восстановление и
# подтверждение по адресу почты, ссылки по адресу источника (перебор токенов).
LOGIN_IP = Limit(5, 60)
LOGIN_ACCOUNT = Limit(10, 600)
REGISTER_IP = Limit(5, 3600)
REGISTER_DOMAIN = Limit(200, 3600)  # популярные почтовые домены — порог грубый, не по адресу
RESET_EMAIL = Limit(3, 3600)
TOKEN_IP = Limit(20, 600)

# KEYS[1] — ключ окна; ARGV: now_ms, window_ms, limit, member. Возвращает 1 — принято, 0 — отказ.
# Скрипты регистрируются один раз (EVALSHA с запасным EVAL — ``register_script``).
_SLIDING_WINDOW = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[1]) - tonumber(ARGV[2]))
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('PEXPIRE', KEYS[1], ARGV[2])
if redis.call('ZCARD', KEYS[1]) > tonumber(ARGV[3]) then return 0 end
return 1
"""

# KEYS[1] — ключ окна; ARGV: now_ms, window_ms. Возвращает {число попыток в окне, score самой
# старой из них или 0} — чтение без учёта попытки (порог по учётной записи считает только отказы).
_WINDOW_STATE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[1]) - tonumber(ARGV[2]))
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
return {redis.call('ZCARD', KEYS[1]), oldest[2] or 0}
"""


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
        self._sliding_window = client.register_script(_SLIDING_WINDOW)
        self._window_state = client.register_script(_WINDOW_STATE)

    async def ensure_available(self) -> None:
        """Проверить доступность Redis; недоступен → ``RateLimitUnavailable`` (fail-closed)."""
        try:
            await self._redis.ping()
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(f"Redis недоступен: {type(exc).__name__}: {exc}") from exc

    async def check(self, counter_key: str, limit: int, window_s: int) -> bool:
        """Учесть попытку и вернуть истину, если за последние ``window_s`` секунд их не больше
        ``limit`` (``counter_key`` — ключ из функций этого модуля). Отклонённые попытки тоже
        считаются. Недоступный Redis → ``RateLimitUnavailable``."""
        now_ms = int(time.time() * 1000)
        try:
            accepted = await self._sliding_window(
                keys=[counter_key],
                args=[now_ms, window_s * 1000, limit, f"{now_ms}:{uuid.uuid4().hex}"],
            )
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(f"Redis недоступен: {type(exc).__name__}: {exc}") from exc
        return bool(int(accepted))

    async def peek(self, counter_key: str, window_s: int) -> int:
        """Число попыток за последние ``window_s`` секунд без учёта новой (порог по учётной
        записи §5.12 считает только отказы: читается до проверки пароля, засчитывается после)."""
        count, _oldest = await self._state(counter_key, window_s)
        return count

    async def retry_after(self, counter_key: str, window_s: int) -> int:
        """Через сколько секунд (вверх) самая старая попытка окна выйдет из него — значение
        ``Retry-After`` при отказе («отказ с задержкой» §5.12); пустое окно — 0."""
        _count, oldest_ms = await self._state(counter_key, window_s)
        if oldest_ms == 0:
            return 0
        remaining_ms = oldest_ms + window_s * 1000 - int(time.time() * 1000)
        return max(0, -(-remaining_ms // 1000))

    async def reset(self, counter_key: str) -> None:
        """Обнулить счётчик (успешный вход снимает порог по учётной записи)."""
        try:
            await self._redis.delete(counter_key)
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(f"Redis недоступен: {type(exc).__name__}: {exc}") from exc

    async def _state(self, counter_key: str, window_s: int) -> tuple[int, int]:
        now_ms = int(time.time() * 1000)
        try:
            count, oldest = await self._window_state(
                keys=[counter_key], args=[now_ms, window_s * 1000]
            )
        except (RedisError, OSError, TimeoutError) as exc:
            raise RateLimitUnavailable(f"Redis недоступен: {type(exc).__name__}: {exc}") from exc
        # score ZSET приходит строкой десятичного числа; отметка в миллисекундах точно
        # представима в float64 (до 2^53 мс ≈ год 2255).
        return int(count), int(float(oldest))
