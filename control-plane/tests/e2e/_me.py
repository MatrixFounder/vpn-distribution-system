"""Общие данные тестов кабинета ``/api/v1/me`` (001.15): список операций и валидные тела —
их переиспользуют тесты кабинета и fail-closed, чтобы новая операция попадала под все стражи по
построению."""

from __future__ import annotations

ME_OPERATIONS: list[tuple[str, str]] = [
    ("GET", "/api/v1/me"),
    ("PATCH", "/api/v1/me"),
    ("DELETE", "/api/v1/me"),
    ("GET", "/api/v1/me/subscription"),
    ("POST", "/api/v1/me/subscription/redeem"),
    ("POST", "/api/v1/me/subscription/reissue"),
    ("GET", "/api/v1/me/traffic"),
    ("GET", "/api/v1/me/onboarding"),
]
# Валидное тело каждой операции (и confirm=true в запросе): 401/403/503 должны наступать раньше
# валидации, а посадка «мутация без CSRF» — давать её штатный ответ, а не 422.
REQUEST_BODY: dict[str, dict[str, str]] = {
    "/api/v1/me": {"language": "ru"},
    "/api/v1/me/subscription/redeem": {"code": "ABCD-EFGH-IJKL"},
}
MUTATIONS = [(method, path) for method, path in ME_OPERATIONS if method != "GET"]
