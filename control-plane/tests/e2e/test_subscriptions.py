"""Сквозные проверки службы подписок на живом стенде (задачи 001.21 и 001.22; UC-02, UC-05,
UC-06; R-24, R-31).

Денежные правила §4.11 проверяются числами примера постановки, а не «каким-нибудь» набором:
**TC-E2E-01** — продление активной подписки (тариф 100 ГБ на 30 дней, израсходовано 60 ГБ, до
окончания 5 дней → доступно 100 ГБ и 35 дней, записи баланса нового периода пусты);
**TC-E2E-02** — смена тарифа переносит расход (израсходовано 60 ГБ, новый тариф 200 ГБ → остаток
140 ГБ, период заменён). Пример взят из §4.11 дословно, потому что он же стоит в критериях
приёмки TASK §5: расхождение с ним — расхождение с постановкой, а не с тестом.

Наборы состояний и источников периода сверяются с перечислениями живой базы (``enum_range``), а
не со списком здесь: копия набора в тесте проверяла бы копию копией. Поведение обработчиков
очереди — модульный страж ``tests/unit/jobs/test_handlers.py``: ставить настоящую задачу
``subscription.*`` в очередь стенда нельзя, её заберёт работающий рядом исполнитель.
"""

from __future__ import annotations

import typing
import uuid

import asyncpg
import httpx
import pytest
from app.domain.subscriptions import PeriodSource, SubscriptionService, SubscriptionState
from app.errors import ApiError

from ._auth import logged_in
from ._catalog import RecordingComposition, catalog, plan

GIB = 1024**3


async def test_tc_e2e_01_renewal_resets_traffic_and_extends_the_term(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-01 (§4.11 «Продление подписки», пример постановки): тариф 100 ГБ на 30 дней,
    израсходовано 60 ГБ, до окончания 5 дней. После продления — 100 ГБ и 35 дней от текущего
    момента, оставшиеся 40 ГБ не перенесены, ``balance_entries`` нового периода пусты."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "basic", days=30, limit=100 * GIB)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())
        first = await service.activate(cabinet.user_id, plan_id, "redeem")
        # Приводим период к условию примера: осталось 5 дней, израсходовано 60 ГБ.
        await conn.execute(
            "update subscription_periods set period_start = now() - interval '25 days', "
            "period_end = now() + interval '5 days', used_billable_bytes = $2 where id = $1",
            first,
            60 * GIB,
        )
        await conn.execute(
            "insert into balance_entries (period_id, source, delta_billable_bytes, ref_key) "
            "values ($1, 'report', $2, 'prev')",
            first,
            60 * GIB,
        )
        assert await service.remaining(cabinet.user_id) == 40 * GIB

        second = await service.renew(cabinet.user_id, "redeem")
        assert second != first, "продление начинает новый период, а не правит прежний"

        row = await conn.fetchrow(
            "select period_start, period_end, traffic_limit_bytes, used_billable_bytes, "
            "extract(epoch from (period_end - now())) / 86400 as days_left "
            "from subscription_periods where id = $1",
            second,
        )
        assert row["traffic_limit_bytes"] == 100 * GIB, "лимит — лимит тарифа"
        assert row["used_billable_bytes"] == 0, "израсходованное обнулено"
        assert 34.9 < float(row["days_left"]) < 35.1, "5 дней прежнего периода плюс 30 нового"
        assert await service.remaining(cabinet.user_id) == 100 * GIB, "40 ГБ не перенесены"
        assert (
            await conn.fetchval("select count(*) from balance_entries where period_id = $1", second)
            == 0
        ), "записи баланса нового периода пусты"
        assert (
            await conn.fetchval(
                "select state from subscriptions where user_id = $1", cabinet.user_id
            )
            == "active"
        )
        # Прежний период и его записи остались на месте: история списаний не переписывается.
        assert (
            await conn.fetchval("select count(*) from balance_entries where period_id = $1", first)
            == 1
        )


async def test_tc_e2e_02_changing_the_plan_carries_the_used_traffic_over(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """TC-E2E-02 (§4.11 «Смена тарифа в середине периода»): израсходовано 60 ГБ, новый тариф
    200 ГБ → остаток 140 ГБ. Израсходованное перенесено (смена не продление и лимит не
    обнуляет), период заменён значениями нового тарифа, записи баланса остались при нём."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        small = await plan(conn, "small", days=30, limit=100 * GIB, device_limit=2)
        large = await plan(conn, "large", days=60, limit=200 * GIB, device_limit=5)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())
        period = await service.activate(cabinet.user_id, small, "admin")
        await conn.execute(
            "update subscription_periods set used_billable_bytes = $2 where id = $1",
            period,
            60 * GIB,
        )
        await conn.execute(
            "insert into balance_entries (period_id, source, delta_billable_bytes, ref_key) "
            "values ($1, 'report', $2, 'used')",
            period,
            60 * GIB,
        )

        await service.change_plan(cabinet.user_id, large, uuid.uuid4())

        row = await conn.fetchrow(
            "select plan_id, traffic_limit_bytes, used_billable_bytes, "
            "extract(epoch from (period_end - period_start)) / 86400 as days "
            "from subscription_periods where id = $1",
            period,
        )
        assert row["plan_id"] == large, "тариф периода заменён"
        assert row["traffic_limit_bytes"] == 200 * GIB, "лимит — нового тарифа"
        assert row["used_billable_bytes"] == 60 * GIB, "израсходованное перенесено"
        assert 59.9 < float(row["days"]) < 60.1, "срок — нового тарифа, от начала периода"
        assert await service.remaining(cabinet.user_id) == 140 * GIB
        assert (
            await conn.fetchval(
                "select device_limit from subscriptions where user_id = $1", cabinet.user_id
            )
            == 5
        ), "снимок лимита устройств обновлён вместе с тарифом"
        assert (
            await conn.fetchval("select count(*) from balance_entries where period_id = $1", period)
            == 1
        ), "записи баланса остались при том же периоде"
        assert (
            await conn.fetchval(
                "select count(*) from subscription_periods where user_id = $1", cabinet.user_id
            )
            == 1
        ), "новый период не заводится"


async def test_adding_traffic_journals_the_balance_and_restores_access(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """§4.11 «Бонусный трафик» и «Восстановление доступа»: начисление пишется в
    ``balance_entries`` и увеличивает остаток ровно на начисленное, а исчерпавшему лимит
    возвращает ``active``. Корректировка администратора — источник ``adjustment`` с причиной
    (``CHECK`` §4.2.4), погашение кода — ``bonus`` без актора. Блокировку администратора
    начисление не снимает."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "basic", days=30, limit=100 * GIB)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())
        period = await service.activate(cabinet.user_id, plan_id, "redeem")
        # Расход заводится как его заводит учёт — записью журнала и счётчиком вместе: иначе
        # тождество «счётчик = сумма записей» ломает сама подготовка теста, а не код.
        await conn.execute(
            "insert into balance_entries (period_id, source, delta_billable_bytes, ref_key) "
            "values ($1, 'report', $2, 'spent')",
            period,
            100 * GIB,
        )
        await conn.execute(
            "update subscription_periods set used_billable_bytes = $2 where id = $1",
            period,
            100 * GIB,
        )
        await service.set_state(cabinet.user_id, "suspended_quota", "лимит исчерпан")
        assert await service.remaining(cabinet.user_id) == 0

        admin = uuid.uuid4()
        await service.add_traffic(cabinet.user_id, 10 * GIB, admin, "докупил трафик")
        assert await service.remaining(cabinet.user_id) == 10 * GIB, "остаток вырос на начисленное"
        assert (
            await conn.fetchval(
                "select state from subscriptions where user_id = $1", cabinet.user_id
            )
            == "active"
        ), "доступ восстановлен (§4.11)"
        entry = await conn.fetchrow(
            "select source, delta_billable_bytes, actor_id, reason, ref_key from balance_entries "
            "where period_id = $1 and source <> 'report'",
            period,
        )
        assert entry["source"] == "adjustment" and entry["actor_id"] == admin
        assert entry["delta_billable_bytes"] == -10 * GIB, "начисление — отрицательная дельта"
        assert entry["reason"] == "докупил трафик" and entry["ref_key"]
        assert await conn.fetchval(
            "select used_billable_bytes = (select sum(delta_billable_bytes) from balance_entries "
            "where period_id = $1) from subscription_periods where id = $1",
            period,
        ), "счётчик — денормализация суммы записей (§4.2.4, M-4)"

        await service.add_traffic(cabinet.user_id, GIB, None, None, ref_key="code-1")
        assert (
            await conn.fetchval("select source from balance_entries where ref_key = 'code-1'")
            == "bonus"
        ), "погашение кода — источник bonus"

        await service.set_state(cabinet.user_id, "suspended_admin", "нарушение AUP")
        await service.add_traffic(cabinet.user_id, GIB, admin, "ещё трафик")
        assert (
            await conn.fetchval(
                "select state from subscriptions where user_id = $1", cabinet.user_id
            )
            == "suspended_admin"
        ), "блокировку администратора начисление не снимает"

        with pytest.raises(ApiError) as refused:
            await service.add_traffic(cabinet.user_id, GIB, admin, None)
        assert refused.value.status == 422, "корректировке нужна причина (CHECK §4.2.4)"
        with pytest.raises(ApiError):
            await service.add_traffic(cabinet.user_id, 0, admin, "ноль")


async def test_expiry_is_idempotent_and_wakes_the_stream_once(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """Истечение переводит в ``expired`` и будит поток состава один раз: планировщик 001.83
    ходит раз в минуту и может принести одного пользователя дважды, а второй перевод разбудил бы
    поток на пустом месте — на каждого истёкшего, каждую минуту."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "basic", days=30, limit=100 * GIB)
        composition = RecordingComposition()
        service = SubscriptionService(cabinet.stand.pool, composition)
        await service.activate(cabinet.user_id, plan_id, "redeem")
        composition.published.clear()

        await service.expire(cabinet.user_id)
        assert (
            await conn.fetchval(
                "select state from subscriptions where user_id = $1", cabinet.user_id
            )
            == "expired"
        )
        assert composition.published == [cabinet.user_id]

        await service.expire(cabinet.user_id)
        assert composition.published == [cabinet.user_id], "повтор состав не будит"


async def test_every_transition_wakes_the_composition_stream(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """Состояние подписки едет на ноды потоком состава (§3.4, §4.11): переход, не разбудивший
    поток, не даёт ни отзыва в пределах Н-14, ни восстановления доступа. Каждый переход
    проверяется поимённо, а не суммой вызовов: перенос публикации с одного перехода на другой
    оставил бы сумму прежней."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        first = await plan(conn, "first", days=30, limit=100 * GIB)
        second = await plan(conn, "second", days=30, limit=200 * GIB)
        composition = RecordingComposition()
        service = SubscriptionService(cabinet.stand.pool, composition)
        user = cabinet.user_id

        for step in (
            lambda: service.activate(user, first, "redeem"),
            lambda: service.renew(user, "admin"),
            lambda: service.change_plan(user, second, uuid.uuid4()),
            lambda: service.add_traffic(user, GIB, None, None),
            lambda: service.set_state(user, "suspended_admin", "проверка"),
            lambda: service.expire(user),
        ):
            composition.published.clear()
            await step()
            assert composition.published == [user], step

        composition.published.clear()
        await service.remaining(user)
        assert composition.published == [], "чтение состав не публикует"


async def test_a_subscription_is_refused_on_an_archived_plan(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """Архивный тариф снят с выдачи (UC-09 A4): новая подписка по нему обошла бы это решение.
    Уже выданные подписки архивный статус не трогает — смена тарифа на архивный тоже отказ."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "live", days=30, limit=100 * GIB)
        archived = await plan(conn, "gone", days=30, limit=100 * GIB)
        await conn.execute("update plans set status = 'archived' where id = $1", archived)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())

        with pytest.raises(ApiError) as refused:
            await service.activate(cabinet.user_id, archived, "redeem")
        assert refused.value.status == 409
        with pytest.raises(ApiError) as unknown:
            await service.activate(cabinet.user_id, uuid.uuid4(), "redeem")
        assert unknown.value.status == 422

        await service.activate(cabinet.user_id, plan_id, "redeem")
        with pytest.raises(ApiError):
            await service.change_plan(cabinet.user_id, archived, uuid.uuid4())


async def test_operations_without_a_subscription_are_refused(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """Продление, смена тарифа и начисление требуют действующего периода: без него отказ 409, а
    не молчаливое «ничего не произошло». ``remaining`` без подписки — ``None``."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "basic", days=30, limit=100 * GIB)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())
        assert await service.remaining(cabinet.user_id) is None
        for call in (
            service.renew(cabinet.user_id, "admin"),
            service.change_plan(cabinet.user_id, plan_id, uuid.uuid4()),
            service.add_traffic(cabinet.user_id, GIB, None, None),
            service.set_state(cabinet.user_id, "active", "нет подписки"),
        ):
            with pytest.raises(ApiError) as refused:
                await call
            assert refused.value.status == 409


async def test_an_unlimited_plan_has_no_remainder(
    pg_dsn: str, redis_url: str, migrate_env: dict[str, str]
) -> None:
    """Unlimited (``traffic_limit_bytes`` NULL, §4.11 «Типы лимитов»): остатка не существует —
    ``None``, а не ноль и не большое число. Расход при этом продолжает записываться."""
    async with logged_in(pg_dsn, redis_url) as cabinet, catalog(pg_dsn, migrate_env) as conn:
        plan_id = await plan(conn, "unlimited", days=30, limit=None)
        service = SubscriptionService(cabinet.stand.pool, RecordingComposition())
        period = await service.activate(cabinet.user_id, plan_id, "admin")
        assert await service.remaining(cabinet.user_id) is None
        await conn.execute(
            "update subscription_periods set used_billable_bytes = $2 where id = $1",
            period,
            10**12,
        )
        assert await service.remaining(cabinet.user_id) is None


async def test_the_states_are_the_enum_of_the_database(pg_dsn: str) -> None:
    """Критерий приёмки 001.21 «состояния определены»: набор домена — это перечисление
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
