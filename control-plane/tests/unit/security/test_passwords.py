"""TC-UNIT-01 задачи 001.12: argon2id с параметрами §7.2, проверка пароля."""

from __future__ import annotations

from app.security.passwords import hash_password, verify_password


def test_verify_matches_only_the_original_password() -> None:
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("correct horse battery stapl", h) is False
    assert verify_password("", h) is False


def test_hash_uses_argon2id_with_section_7_2_parameters() -> None:
    h = hash_password("пароль-юникод-🔐")
    assert h.startswith("$argon2id$v=19$m=65536,t=3,p=4$"), h  # 64 MiB, t = 3, p = 4
    assert verify_password("пароль-юникод-🔐", h) is True


def test_hashes_are_salted() -> None:
    assert hash_password("same") != hash_password("same")


def test_garbage_hash_is_false_not_error() -> None:
    assert verify_password("anything", "not-a-hash") is False
    assert verify_password("anything", "") is False
