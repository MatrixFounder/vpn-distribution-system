"""Сквозные проверки маршрутов ``/api/v1/auth`` на заглушках (задача 001.13): UC-02 шаги 1–6 —
регистрация → подтверждение → вход; UC-15 — запрос восстановления → подтверждение → вход.

TC-E2E-01: регистрация → 201 ``{"status": "unconfirmed"}``. TC-E2E-02: вход → 200 и cookie ``sid``
``HttpOnly; Secure; SameSite=Lax`` (атрибуты разбираются, не ищутся подстрокой; снятие — теми же
атрибутами). TC-E2E-03: запрос восстановления → 202 для любого адреса, ответы для существующего
и несуществующего адреса неотличимы — статус, байты тела и все заголовки кроме ``Date`` (UC-15
A1). Плюс: семь маршрутов в OpenAPI со схемами, валидация тел (422 единого формата, лишние поля и
управляющие символы отклоняются), контракт «``authenticate → None`` = 401 без cookie» для 001.14.
"""

from __future__ import annotations

from http.cookies import SimpleCookie

import httpx
import pytest
from app.domain.users import STUB_USER_ID, UserService

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
    "password": "correct horse battery",
    "aup_version": "2026-09",
    "lang": "ru",
}
VOLATILE_HEADERS = {"date"}  # различаются между любыми двумя ответами по построению


def cookie_attributes(header: str) -> tuple[str, dict[str, str]]:
    """Разобрать Set-Cookie: значение и атрибуты в нижнем регистре (без подстрочных догадок)."""
    jar: SimpleCookie = SimpleCookie()
    jar.load(header)
    assert list(jar) == ["sid"], header
    morsel = jar["sid"]
    attributes = {
        key: str(value).lower() for key, value in morsel.items() if value not in ("", False)
    }
    return morsel.value, attributes


def stable_headers(response: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower() not in VOLATILE_HEADERS}


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
    assert {"RegisterIn", "LoginIn", "ResetConfirmIn"} <= set(schema["components"]["schemas"])
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
    }, "фиксированные ответы закреплены в схеме"


async def test_uc02_register_verify(app_client: httpx.AsyncClient) -> None:
    """UC-02 шаги 3–6 на заглушках: TC-E2E-01."""
    register = await app_client.post("/api/v1/auth/register", json=VALID_REGISTER)
    assert register.status_code == 201 and register.json() == {"status": "unconfirmed"}
    assert "set-cookie" not in register.headers, "регистрация не создаёт сессию"
    verify = await app_client.post("/api/v1/auth/verify", json={"token": "x" * 32})
    assert verify.status_code == 200 and verify.json() == {"status": "confirmed"}


async def test_uc02_login_sets_session_cookie(app_client: httpx.AsyncClient) -> None:
    """TC-E2E-02: cookie ``sid`` с атрибутами §7.1, значение случайное и непрозрачное."""
    login = await app_client.post(
        "/api/v1/auth/login",
        json={"email": VALID_REGISTER["email"], "password": "correct horse battery"},
    )
    assert login.status_code == 200 and login.json() == {"status": "ok"}
    sid, attributes = cookie_attributes(login.headers["set-cookie"])
    assert len(sid) >= 32, "значение cookie — непрозрачное, ≥ 128 бит"
    assert attributes["httponly"] == "true" and attributes["secure"] == "true"
    assert attributes["samesite"] == "lax" and attributes["path"] == "/"
    assert int(attributes["max-age"]) >= 3600
    second = await app_client.post(
        "/api/v1/auth/login", json={"email": "a@b.io", "password": "x" * 8}
    )
    assert cookie_attributes(second.headers["set-cookie"])[0] != sid, "значение не фиксировано"


@pytest.mark.parametrize("path", ["/api/v1/auth/logout", "/api/v1/auth/logout-all"])
async def test_logout_clears_cookie_with_same_attributes(
    app_client: httpx.AsyncClient, path: str
) -> None:
    """Снятие cookie — теми же Path/HttpOnly/Secure/SameSite, иначе браузер её не сопоставит."""
    login = await app_client.post(
        "/api/v1/auth/login", json={"email": "a@b.io", "password": "x" * 8}
    )
    _, issued = cookie_attributes(login.headers["set-cookie"])
    logout = await app_client.post(path)
    assert logout.status_code == 204
    value, cleared = cookie_attributes(logout.headers["set-cookie"])
    assert value == "" or cleared.get("max-age") == "0" or "expires" in cleared
    assert cleared.get("max-age") == "0" or "expires" in cleared, "cookie снимается"
    for key in ("path", "httponly", "secure", "samesite"):
        assert cleared.get(key) == issued[key], (key, cleared, issued)


async def test_uc15_reset_request_is_indistinguishable(app_client: httpx.AsyncClient) -> None:
    """TC-E2E-03 и UC-15 A1: 202 и полностью одинаковый ответ для зарегистрированного и любого
    другого адреса; подтверждение → 200; вход после смены пароля."""
    existing = await app_client.post(
        "/api/v1/auth/reset-request", json={"email": VALID_REGISTER["email"]}
    )
    unknown = await app_client.post(
        "/api/v1/auth/reset-request", json={"email": "nobody@example.net"}
    )
    assert existing.status_code == unknown.status_code == 202
    assert existing.content == unknown.content == b'{"status":"accepted"}'
    assert stable_headers(existing) == stable_headers(unknown), "ни один заголовок не различает"
    assert "set-cookie" not in existing.headers

    confirm = await app_client.post(
        "/api/v1/auth/reset-confirm", json={"token": "t" * 32, "password": "new password 123"}
    )
    assert confirm.status_code == 200 and confirm.json() == {"status": "password_changed"}
    login = await app_client.post(
        "/api/v1/auth/login",
        json={"email": VALID_REGISTER["email"], "password": "new password 123"},
    )
    assert login.status_code == 200 and "sid=" in login.headers["set-cookie"]


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
        ("/api/v1/auth/reset-request", {"email": "ad\x00min@b.io"}, ["body", "email"]),
        ("/api/v1/auth/reset-request", {"email": "a@b.io\r\nBcc: x@y.z"}, ["body", "email"]),
        ("/api/v1/auth/verify", {"token": "t" * 32, "is_admin": True}, ["body", "is_admin"]),
        ("/api/v1/auth/reset-request", {"email": "a@b.io", "extra": 1}, ["body", "extra"]),
        (
            "/api/v1/auth/reset-confirm",
            {"token": "t" * 32, "password": "x" * 8, "role": "root"},
            ["body", "role"],
        ),
        (
            "/api/v1/auth/reset-confirm",
            {"token": "t" * 32, "password": "short"},
            ["body", "password"],
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


async def test_login_rejects_when_authenticate_returns_none(
    app_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Контракт маршрута для 001.14: ``authenticate → None`` — 401 ``invalid_credentials`` и
    никакой cookie (заглушка 001.13 ``None`` не возвращает — проверяется подменой)."""

    async def deny(self: UserService, email: str, password: str) -> None:
        return None

    monkeypatch.setattr(UserService, "authenticate", deny)  # класс, который отдаёт зависимость
    denied = await app_client.post(
        "/api/v1/auth/login", json={"email": "a@b.io", "password": "wrong password"}
    )
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_credentials"
    assert "set-cookie" not in denied.headers


async def test_user_service_stub_contract() -> None:
    """Сигнатуры UserService из контракта; заглушки возвращают фиксированные значения."""
    service = UserService()
    assert await service.register("a@b.io", "password", "2026-09", "en") == STUB_USER_ID
    assert await service.authenticate("a@b.io", "password") == STUB_USER_ID
    await service.verify_email("t" * 32)  # заглушки без результата: успех — отсутствие ошибки
    await service.request_reset("a@b.io")
    await service.confirm_reset("t" * 32, "password")
