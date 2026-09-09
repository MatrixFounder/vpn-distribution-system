"""TC-UNIT-01 задачи 001.14: нормализация адреса §16.8 и вспомогательные функции домена."""

from __future__ import annotations

import hashlib

from app.domain import users
from app.domain.users import NoCaptchaProvider, hash_token, normalize_email


def test_normalize_email_lowercases_and_strips() -> None:
    assert normalize_email("Foo.Bar+x@Example.com") == "foo.bar+x@example.com"
    assert normalize_email("  User@MAIL.RU\n") == "user@mail.ru"
    assert normalize_email("a@b.io") == "a@b.io"


def test_hash_token_is_sha256_hex() -> None:
    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_token("abc") != hash_token("abd")


async def test_captcha_answer_asks_provider_once_and_never_for_missing_token() -> None:
    """Ответ провайдера одноразовый: ``CaptchaAnswer`` запоминает первую проверку; без ответа
    провайдер не спрашивается (ревью 001.14, раунд 3)."""

    class Counting:
        calls = 0

        async def verify(self, token: str | None, ip: str | None) -> bool:
            self.calls += 1
            return True

    provider = Counting()
    answer = users.CaptchaAnswer(provider, "a", "203.0.113.1")
    assert answer.available is True
    assert await answer.verified() is True and await answer.verified() is True
    assert provider.calls == 1
    missing = users.CaptchaAnswer(provider, None, "203.0.113.1")
    assert await missing.verified() is False and provider.calls == 1
    assert users.CaptchaAnswer(users.NoCaptchaProvider(), "a", None).available is False


async def test_no_captcha_provider_fails_closed() -> None:
    assert await NoCaptchaProvider().verify("any-token", "203.0.113.1") is False
    assert await NoCaptchaProvider().verify(None, None) is False
