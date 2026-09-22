"""Сквозные проверки службы подписок на заглушках (задача 001.21; UC-02, UC-06) на живом стенде.

TC-E2E-01: ``activate`` возвращает фиксированный ``PeriodId``, и кабинет показывает по этому же
пользователю состояние ``active`` с тем же периодом — период, возвращённый службой, и период,
который видит пользователь, обязаны совпадать и на заглушках. Рядом — прогон UC-06 (продление,
смена тарифа A1, начисление трафика A4, истечение A3) по контракту заглушки.

Наборы состояний и источников периода сверяются с перечислениями живой базы
(``enum_range``), а не со списком в тесте: это критерий приёмки «состояния определены», и копия
набора в тесте проверяла бы копию копией. Поведение обработчиков очереди — модульный страж
``tests/unit/jobs/test_handlers.py``: ставить настоящую задачу ``subscription.*`` в очередь
стенда нельзя, её заберёт работающий рядом исполнитель.

Правила §4.11 (продление обнуляет расход, смена тарифа его переносит, истечение) заглушка не
исполняет — их приносят 001.22 и 001.83 в этот же файл.
"""

from __future__ import annotations

import typing
import uuid

import asyncpg
import httpx
from app.domain.subscriptions import (
    STUB_PERIOD_ID,
    STUB_REMAINING_BYTES,
    PeriodSource,
    SubscriptionService,
    SubscriptionState,
)

from ._auth import logged_in


async def test_activation_answers_the_period_the_cabinet_shows(pg_dsn: str, redis_url: str) -> None:
    """TC-E2E-01: активация возвращает период; состояние — ``active`` (заглушка)."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        subscriptions = SubscriptionService(cabinet.stand.pool)
        period = await subscriptions.activate(
            cabinet.user_id, uuid.UUID("00000000-0000-7000-8000-0000000000d2"), "redeem"
        )
        assert period == STUB_PERIOD_ID

        response = await cabinet.client.get("/api/v1/me/subscription")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "active"
        assert body["period"]["id"] == str(period), "кабинет показывает тот же период"
        assert body["remaining"] == await subscriptions.remaining(cabinet.user_id)


async def test_uc06_walk_on_stubs(pg_dsn: str, redis_url: str) -> None:
    """UC-06 на заглушках: продление (шаги 1–4), смена тарифа администратором (A1), начисление
    трафика (A4) и истечение (A3) вызываются в порядке сценария и отвечают контрактом заглушки.

    Через кабинет сценарий не проходит: маршрутов продления и смены тарифа у `/me` нет — они
    административные (001.49/001.50), а погашение кода доводит до службы 001.20. Поэтому шаги
    идут через службу на живом пуле стенда. Что заглушка ничего не пишет, здесь не проверяется:
    ответ кабинета от службы пока не зависит (связать их обязана 001.22), и сверка ответа до и
    после была бы сравнением константы с собой; отсутствие обращений к базе закреплено часовым
    в `tests/unit/domain/test_subscriptions.py`."""
    async with logged_in(pg_dsn, redis_url) as cabinet:
        subscriptions = SubscriptionService(cabinet.stand.pool)
        plan = uuid.UUID("00000000-0000-7000-8000-0000000000d2")
        admin = uuid.UUID("00000000-0000-7000-8000-0000000000d3")

        assert await subscriptions.renew(cabinet.user_id, "redeem") == STUB_PERIOD_ID
        await subscriptions.change_plan(cabinet.user_id, plan, admin)
        await subscriptions.add_traffic(cabinet.user_id, 10 * 1024**3, admin, "A4")
        await subscriptions.set_state(cabinet.user_id, "suspended_quota", "лимит")
        await subscriptions.expire(cabinet.user_id)
        assert await subscriptions.remaining(cabinet.user_id) == STUB_REMAINING_BYTES


async def test_the_states_are_the_enum_of_the_database(pg_dsn: str) -> None:
    """Критерий приёмки «состояния определены»: набор домена — это перечисление
    ``subscription_state`` базы. Третья копия набора (база, OpenAPI кабинета, домен) с иным
    составом развела бы состояние, которое едет на ноды, и состояние, которое видит
    пользователь."""
    conn = await asyncpg.connect(pg_dsn)
    try:
        states = await conn.fetchval("select enum_range(null::subscription_state)::text[]")
        sources = await conn.fetchval("select enum_range(null::period_source)::text[]")
    finally:
        await conn.close()
    assert list(typing.get_args(SubscriptionState)) == list(states)
    assert list(typing.get_args(PeriodSource)) == list(sources)


async def test_the_cabinet_state_comes_from_the_same_set(app_client: httpx.AsyncClient) -> None:
    """Та же проверка со стороны контракта: ``SubscriptionOut.state`` в OpenAPI перечисляет
    ровно состояния домена — кабинет и домен ссылаются на один литерал, а не на два."""
    schema = (await app_client.get("/openapi.json")).json()
    subscription = schema["components"]["schemas"]["SubscriptionOut"]
    assert set(subscription["properties"]["state"]["enum"]) == set(
        typing.get_args(SubscriptionState)
    )
