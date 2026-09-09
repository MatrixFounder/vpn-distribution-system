"""Раздел ``/api/v1/me`` — кабинет пользователя (interfaces.md §5.1; постановка §4.2, §4.12,
§17.6; UC-14, UC-16): профиль, подписка, статистика, мастер первого подключения, удаление
аккаунта.

Задача 001.15 — восемь маршрутов со схемами ответов и заглушками: ``ProfileService``
(``domain/profile.py``), ``user_traffic`` (``accounting/stats.py``), фиксированная подписка
(``STUB_SUBSCRIPTION``). Все маршруты — под сессией пользователя (``current_user``, cookie
``sid`` §7.1; без сессии 401 ``unauthenticated``), мутации — под CSRF (§7.3, ``require_csrf``),
раздел целиком — fail-closed без Redis (``redis_required`` при включении роутера). Действия с
последствиями для устройств и данных требуют подтверждения ``?confirm=true``: перевыпуск токена
(UC-16 шаг 4 — предупреждение о разрыве всех устройств) и удаление аккаунта (UC-14 шаг 1) — без
него 409 ``confirmation_required``. Логика — 001.16 (профиль, статистика, перевыпуск,
онбординг), 001.17 (удаление), активация кодов — 001.20.
"""

from __future__ import annotations

import uuid
import zoneinfo
from typing import Annotated, Literal
from urllib.parse import quote

import asyncpg
from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.accounting.stats import STUB_STATS, PeriodRef, TrafficStats, user_traffic
from app.config import Settings
from app.db.pool import db_pool, get_pool
from app.domain.profile import Language, Profile, ProfileService
from app.errors import ApiError
from app.security.csrf import require_csrf
from app.security.deps import CurrentUser, current_user

router = APIRouter(prefix="/me", tags=["me"])

# Имена IANA — один раз на процесс: ``available_timezones()`` обходит tzdata на диске (~10 мс),
# на запросе это блокировало бы цикл событий (ревью 001.15, S-1).
IANA_TIMEZONES = frozenset(zoneinfo.available_timezones())

SubscriptionState = Literal["none", "active", "suspended_quota", "suspended_admin", "expired"]
Platform = Literal["ios", "android", "macos", "windows", "linux", "unknown"]


class StrictModel(BaseModel):
    """Тела запросов кабинета: неизвестные поля — 422, краевые пробелы обрезаются (как /auth)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfileUpdateIn(StrictModel):
    """Изменяемые поля профиля (§4.2, §17.8): язык, часовой пояс IANA, согласие на объявления."""

    language: Language | None = None
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    announce_consent: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _iana(cls, value: str | None) -> str | None:
        if value is not None and value not in IANA_TIMEZONES:
            raise ValueError("часовой пояс должен быть именем IANA, например Europe/Amsterdam")
        return value


class RedeemIn(StrictModel):
    code: Annotated[str, StringConstraints(min_length=8, max_length=64)]


class PlanRef(BaseModel):
    id: uuid.UUID
    name: str


class SubscriptionOut(BaseModel):
    """Состояние подписки (§4.2, UC-16 шаг 1): тариф, состояние, период, лимит и остаток,
    ссылки подписки на двух доменах (§5.6) и лимит устройств (§4.13)."""

    state: SubscriptionState
    plan: PlanRef | None
    period: PeriodRef | None
    limit: int | None = Field(default=None, ge=0, description="лимит периода; None — безлимит")
    used_billable: int = Field(ge=0)
    remaining: int | None = Field(default=None, ge=0)
    device_limit: int | None = Field(default=None, ge=1)
    links: list[str] = Field(description="https://{домен}/s/{token} на каждом домене подписки")
    reason: str | None = Field(
        default=None, description="причина и действие для suspended_*/expired (UC-16 A1)"
    )


class ClientLink(BaseModel):
    name: str
    platforms: list[Platform]
    install_url: str


class ConnectionCheck(BaseModel):
    status: Literal["unknown", "ok", "failed"]


class OnboardingOut(BaseModel):
    """Мастер первого подключения (§17.6): платформа, клиенты §4.2, импорт одним действием,
    проверка соединения."""

    platform: Platform
    clients: list[ClientLink]
    subscription_links: list[str]
    import_url: str = Field(description="ссылка импорта подписки в клиент одним действием")
    qr_payload: str = Field(description="что кодировать в QR — ссылка подписки")
    check: ConnectionCheck


User = Annotated[CurrentUser, Depends(current_user)]


async def get_profile_service() -> ProfileService:
    return ProfileService(await get_pool())


Profiles = Annotated[ProfileService, Depends(get_profile_service)]
Pool = Annotated[asyncpg.Pool, Depends(db_pool)]


def confirmation_required(action: str, consequence: str) -> ApiError:
    """409 для действий с последствиями без ``?confirm=true`` (UC-16 шаг 4, UC-14 шаг 1)."""
    return ApiError(
        "confirmation_required",
        f"{action}: {consequence} — подтвердите запросом с confirm=true",
        status=409,
        details={"warning": consequence, "confirm": "confirm=true"},
    )


# --- фиксированные значения заглушек ------------------------------------------------------------

STUB_PLAN = PlanRef(id=uuid.UUID("00000000-0000-7000-8000-0000000000c1"), name="Standard 100 GB")
STUB_TOKEN = "stub-subscription-token-0000000000000000000000"  # noqa: S105 — заглушка, не секрет
STUB_TOKEN_REISSUED = "stub-subscription-token-1111111111111111111111"  # noqa: S105
CLIENTS = [
    ClientLink(
        name="Shadowrocket", platforms=["ios"], install_url="https://apps.apple.com/app/id932747118"
    ),
    ClientLink(
        name="Streisand",
        platforms=["ios", "macos"],
        install_url="https://apps.apple.com/app/id6450534064",
    ),
    ClientLink(
        name="v2rayNG",
        platforms=["android"],
        install_url="https://github.com/2dust/v2rayNG/releases",
    ),
    ClientLink(
        name="Hiddify",
        platforms=["ios", "android", "macos", "windows", "linux"],
        install_url="https://hiddify.com/",
    ),
    ClientLink(
        name="sing-box",
        platforms=["ios", "android", "macos", "windows", "linux"],
        install_url="https://sing-box.sagernet.org/clients/",
    ),
]


def subscription_links(token: str) -> list[str]:
    """Ссылки подписки на всех доменах ``SUBSCRIPTION_DOMAINS`` (§4.2: два домена в разных
    зонах регистрации). Читается окружение (``Settings.read()``), не файлы секретов; отсутствие
    доменов — ошибка конфигурации, а не пустой ответ 200 (ревью 001.15, L-1)."""
    domains = Settings.read().subscription_domains
    if not domains:  # резерв: для роли api пустой список отвергает валидатор Settings на старте
        raise RuntimeError("SUBSCRIPTION_DOMAINS не задан: кабинету нечего показать (§4.2)")
    return [f"https://{domain}/s/{token}" for domain in domains]


def stub_subscription(token: str = STUB_TOKEN) -> SubscriptionOut:
    """Подписка заглушки: активна, тариф 100 ГБ, списано 20 ГБ (согласовано с ``STUB_STATS``)."""
    return SubscriptionOut(
        state="active",
        plan=STUB_PLAN,
        period=STUB_STATS.period,
        limit=STUB_STATS.limit,
        used_billable=STUB_STATS.billable,
        remaining=STUB_STATS.remaining,
        device_limit=STUB_STATS.device_limit,
        links=subscription_links(token),
    )


# --- профиль -------------------------------------------------------------------------------------


@router.get("", response_model=Profile, summary="Профиль пользователя (§4.2)")
async def profile(user: User, profiles: Profiles) -> Profile:
    return await profiles.get(user.id)


@router.patch(
    "",
    response_model=Profile,
    summary="Изменить язык, часовой пояс, согласие (UC-16 шаг 7)",
    dependencies=[Depends(require_csrf)],
)
async def update_profile(body: ProfileUpdateIn, user: User, profiles: Profiles) -> Profile:
    return await profiles.update(
        user.id,
        language=body.language,
        timezone=body.timezone,
        announce_consent=body.announce_consent,
    )


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить аккаунт (UC-14; требует confirm=true)",
    dependencies=[Depends(require_csrf)],
)
async def delete_account(
    user: User,
    profiles: Profiles,
    confirm: Annotated[bool, Query(description="подтверждение необратимого удаления")] = False,
) -> None:
    if not confirm:
        raise confirmation_required(
            "удаление аккаунта",
            "учётная запись не восстанавливается, подписка и доступ на устройствах прекращаются",
        )
    await profiles.delete_account(user.id)


# --- подписка ------------------------------------------------------------------------------------


@router.get(
    "/subscription", response_model=SubscriptionOut, summary="Состояние подписки (UC-16 шаг 1)"
)
async def subscription(user: User) -> SubscriptionOut:
    return stub_subscription()


@router.post(
    "/subscription/redeem",
    response_model=SubscriptionOut,
    summary="Активировать Redeem-код (UC-02 шаги 7–10; логика — 001.20)",
    dependencies=[Depends(require_csrf)],
)
async def redeem(body: RedeemIn, user: User) -> SubscriptionOut:
    return stub_subscription()


@router.post(
    "/subscription/reissue",
    response_model=SubscriptionOut,
    summary="Перевыпустить токен подписки (UC-16 шаги 4–5; требует confirm=true)",
    dependencies=[Depends(require_csrf)],
)
async def reissue(
    user: User,
    confirm: Annotated[bool, Query(description="подтверждение разрыва всех устройств")] = False,
) -> SubscriptionOut:
    if not confirm:
        raise confirmation_required(
            "перевыпуск токена",
            "прежняя ссылка перестанет работать немедленно, все устройства будут отключены",
        )
    return stub_subscription(STUB_TOKEN_REISSUED)


# --- статистика и онбординг ----------------------------------------------------------------------


@router.get("/traffic", response_model=TrafficStats, summary="Статистика трафика (§4.12)")
async def traffic(
    user: User,
    pool: Pool,
    period: Annotated[
        uuid.UUID | None, Query(description="период подписки; без него — текущий")
    ] = None,
) -> TrafficStats:
    async with pool.acquire() as conn:  # обвязка та же, что у логики 001.16
        return await user_traffic(conn, user.id, period)


@router.get(
    "/onboarding", response_model=OnboardingOut, summary="Мастер первого подключения (§17.6)"
)
async def onboarding(user: User) -> OnboardingOut:
    links = subscription_links(STUB_TOKEN)
    first = links[0]
    return OnboardingOut(
        platform="unknown",  # определение по User-Agent — 001.16
        clients=CLIENTS,
        subscription_links=links,
        import_url=f"sing-box://import-remote-profile?url={quote(first, safe='')}",
        qr_payload=first,
        check=ConnectionCheck(status="unknown"),
    )
