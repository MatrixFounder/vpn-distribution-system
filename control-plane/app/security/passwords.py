"""Хеширование паролей — argon2id с параметрами security.md §7.2 (m = 64 MiB, t = 3, p = 4).
Только вызовы библиотеки ``argon2-cffi``: собственной логики нет (задача 001.12)."""

from __future__ import annotations

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

# §7.2: память 64 MiB, три прохода, четыре потока; тип argon2id.
_hasher = PasswordHasher(memory_cost=64 * 1024, time_cost=3, parallelism=4, type=Type.ID)


def hash_password(p: str) -> str:
    """Хеш в формате PHC (``$argon2id$v=19$m=65536,t=3,p=4$…``), соль библиотека берёт сама."""
    return _hasher.hash(p)


def verify_password(p: str, h: str) -> bool:
    """Истина, если пароль соответствует хешу; несовпадение и негодный хеш — ложь."""
    try:
        return _hasher.verify(h, p)
    except VerificationError, InvalidHashError:  # несовпадение; хеш не argon2 (ValueError)
        return False
