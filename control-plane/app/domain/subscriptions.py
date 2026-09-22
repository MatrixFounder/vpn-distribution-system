"""Подписка пользователя: состояние, период и переходы жизненного цикла (постановка §4.10,
§4.11 «Продление подписки», «Смена тарифа в середине периода», «Бонусный трафик»,
«Восстановление доступа»; data-model.md §4.2.4 ``subscriptions``, ``subscription_periods``,
``balance_entries``; UC-02, UC-05, UC-06; R-24, R-31).

Задача 001.22 — логика поверх схемы 001.07 и тарифов 001.19. Денежные правила §4.11 различаются
знаком, и путать их нельзя:

- **продление** начинает новый период: срок от даты окончания, если подписка активна, и от
  текущего момента, если истекла; лимит равен лимиту тарифа, израсходованное обнуляется,
  неиспользованный остаток прежнего периода не переносится. Начало считает
  ``next_period_start`` — чистая функция, потому что это правило, а не запрос;
- **смена тарифа** периодом не является: срок и лимит заменяются значениями нового тарифа,
  израсходованное **переносится**, прорации нет (платежей в MVP нет, О-2). Поэтому строка
  периода правится на месте, а не заводится новая: вместе с новой уехали бы и записи
  ``balance_entries``, которые называют прежнюю.

Что в модуле настоящее сверх записи в базу:

- **набор ``SubscriptionState``** — перечисление ``subscription_state`` базы целиком и в том же
  порядке (§4.2.4); кабинет берёт литерал отсюда, а страж сверяет его с ``enum_range`` живой
  базы, а не со списком в тесте;
- **каждый переход публикует состав**: состояние подписки едет на ноды потоком состава §3.4, и
  переход, не разбудивший поток, не даёт ни отзыва в пределах Н-14, ни восстановления. Вызов —
  ``CompositionService.publish_user``, как и ожидает 001.29 («вызывается из
  ``SubscriptionService``»); чтение (``remaining``) состав не публикует. Ставить вместо вызова
  задачу очереди (outbox §5.4), как предполагало примечание 001.21, здесь не стали: описание
  001.29 называет прямой вызов, а ключ идемпотентности задачи ``composition.publish_user``
  (``publish:{node_id}:{users_seq}``) построен по ноде и последовательности — их знает 001.29,
  а не эта служба;
- **начисление трафика — запись журнала баланса, а не правка лимита.** §4.11 говорит «бонус
  увеличивает лимит текущего периода», §4.2.4 — что ``used_billable_bytes`` есть денормализация
  суммы ``balance_entries``, среди источников которой стоят ``bonus`` и ``adjustment``. Выбрано
  второе: начисление пишется отрицательной дельтой и уменьшает счётчик, отчего остаток
  ``лимит − израсходовано`` растёт ровно на начисленное — эффект §4.11 достигнут, инвариант M-4
  («изменение баланса журналируется, счётчик — денормализация») сохранён, а источники ``bonus``
  и ``adjustment`` перестают быть значениями перечисления, которые никто не пишет. Пользователю
  это не видно: «Billable Traffic» §4.12 кабинет берёт из таблиц учёта (``traffic_hourly``), а
  не из этого счётчика. Обратное решение — правка ``traffic_limit_bytes`` — оставило бы
  ``bonus`` без единой записи и разошлось бы с §4.2.4.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal, NamedTuple

import asyncpg

from app.db.pool import transaction
from app.domain.composition import CompositionService
from app.errors import ApiError

# Перечисление ``subscription_state`` data-model.md §4.2.4 целиком: ``none`` — подписки не было,
# остальные четыре совпадают по имени с состоянием пользователя на ноде (§4.11) и едут в поток
# состава. ``removed`` из ``user_node_state`` сюда не входит — это состояние строки на ноде.
SubscriptionState = Literal["none", "active", "suspended_quota", "suspended_admin", "expired"]
# Перечисление ``period_source`` data-model.md §4.2.4: чем заведён период.
PeriodSource = Literal["redeem", "admin", "order"]
# Перечисление ``balance_source`` §4.2.4: чем вызвано изменение баланса периода.
BalanceSource = Literal["report", "adjustment", "bonus", "late_report"]
# ``subscription_periods.id`` (uuid v7): период — носитель срока, лимита и всех списаний.
PeriodId = uuid.UUID


class Period(NamedTuple):
    """Текущий период подписки вместе с состоянием — то, что нужно каждому переходу."""

    id: PeriodId
    plan_id: uuid.UUID
    state: SubscriptionState
    period_start: dt.datetime
    period_end: dt.datetime
    traffic_limit_bytes: int | None
    used_billable_bytes: int


def next_period_start(
    state: SubscriptionState, period_end: dt.datetime | None, now: dt.datetime
) -> dt.datetime:
    """Начало нового периода при продлении (§4.11 «Продление подписки», обе строки таблицы):
    у активной подписки — дата окончания текущего периода, у истёкшей — текущий момент.

    Чистая функция, потому что это правило, а не запрос: «продление начинает новый период
    независимо от того, активна подписка или истекла», и разница только в точке отсчёта.
    Состояния, снимающие доступ (``suspended_quota``, ``suspended_admin``), продлеваются как
    активные: доступ снят лимитом или администратором, а срок продолжает идти — иначе продление
    возвращало бы заблокированному пользователю ещё и потерянные дни. Дата окончания в прошлом
    при ещё не переведённом состоянии (планировщик 001.83 ходит раз в минуту) тоже даёт текущий
    момент: иначе новый период начался бы задним числом и часть его срока сгорела бы сразу.
    """
    if state == "none" or period_end is None or period_end <= now:
        return now
    return period_end


def remaining_of(limit: int | None, used: int) -> int | None:
    """Остаток лимита: ``None`` при Unlimited, иначе не меньше нуля (см. ``remaining``)."""
    if limit is None:
        return None
    return max(0, limit - used)


# Текущий период и состояние: две готовые формы вместо склейки строки — запрос не собирается
# из переменных, и ``ruff`` не приходится уговаривать исключением.
_CURRENT = (
    "select s.state, p.id, p.plan_id, p.period_start, p.period_end, p.traffic_limit_bytes, "
    "p.used_billable_bytes from subscriptions s "
    "left join subscription_periods p on p.id = s.current_period_id where s.user_id = $1"
)
_CURRENT_LOCKED = f"{_CURRENT} for no key update of s"


def _no_subscription() -> ApiError:
    return ApiError("no_subscription", "у пользователя нет подписки", status=409)


class SubscriptionService:
    """Подписка поверх пула asyncpg (001.22). ``composition`` — поток состава, который будит
    каждый переход; подменяется в тестах, по умолчанию строится над тем же пулом."""

    def __init__(self, pool: Any, composition: CompositionService | None = None) -> None:
        self._pool = pool
        self._composition = composition or CompositionService(pool)

    async def activate(
        self,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        source: PeriodSource,
        *,
        source_id: uuid.UUID | None = None,
    ) -> PeriodId:
        """Активировать подписку: период тарифа от текущего момента, лимит — лимит тарифа,
        состояние ``active`` (§4.11 «Продление подписки», строка «Подписка истекла»; UC-02
        шаг 9). Возвращает идентификатор заведённого периода: бонус того же погашения кода
        (§4.14) пишется в ``balance_entries`` уже этого периода, и вызывающему (001.20) нужен
        его ключ.

        ``source_id`` — что породило период (код, действие администратора, заказ): колонка
        ``subscription_periods.source_id`` без него осталась бы пустой навсегда."""
        async with transaction(self._pool) as conn:
            plan = await self._plan(conn, plan_id)
            period_id = await self._open_period(
                conn, user_id, plan_id, plan, source, source_id, at=None
            )
            await self._set_current(conn, user_id, period_id, "active", plan["device_limit"])
        await self._composition.publish_user(user_id)
        return period_id

    async def renew(
        self,
        user_id: uuid.UUID,
        source: PeriodSource,
        *,
        source_id: uuid.UUID | None = None,
    ) -> PeriodId:
        """Продлить подписку действующим тарифом: активная — новый период от даты окончания,
        истёкшая — от текущего момента; лимит равен лимиту тарифа, израсходованное обнуляется,
        неиспользованный остаток прежнего периода не переносится (§4.11 «Продление подписки»,
        обе строки таблицы; правило одно для администратора и для Redeem-кода).

        Пример §4.11: тариф 100 ГБ на 30 дней, израсходовано 60 ГБ, до окончания 5 дней →
        после продления доступно 100 ГБ и 35 дней; оставшиеся 40 ГБ не переносятся."""
        async with transaction(self._pool) as conn:
            current = await self._current(conn, user_id, lock=True)
            if current is None:
                raise _no_subscription()
            plan = await self._plan(conn, current.plan_id)
            now: dt.datetime = await conn.fetchval("select now()")
            start = next_period_start(current.state, current.period_end, now)
            period_id = await self._open_period(
                conn, user_id, current.plan_id, plan, source, source_id, at=start
            )
            await self._set_current(conn, user_id, period_id, "active", plan["device_limit"])
        await self._composition.publish_user(user_id)
        return period_id

    async def change_plan(self, user_id: uuid.UUID, plan_id: uuid.UUID, actor: uuid.UUID) -> None:
        """Сменить тариф в середине периода: срок и лимит заменяются значениями нового тарифа,
        израсходованное переносится, прорации нет — смена не является продлением (§4.11 «Смена
        тарифа в середине периода»). ``actor`` — администратор: приёма платежей в MVP нет (О-2),
        самостоятельной смены тарифа тоже, а действие пишется в Audit Log (§4.16). Состав
        серверов при сужении групп доступа меняется в пределах Н-13 потоком состава (UC-06 A2).

        Строка периода правится на месте: завести новый период значило бы оставить записи
        ``balance_entries`` при прежнем, а израсходованное обязано перенестись (TC-E2E-02).
        Срок считается от начала периода, а не от момента смены: иначе смена тарифа добавляла бы
        дни, то есть работала бы как продление, которым §4.11 её прямо не считает."""
        async with transaction(self._pool) as conn:
            current = await self._current(conn, user_id, lock=True)
            if current is None:
                raise _no_subscription()
            plan = await self._plan(conn, plan_id)
            await conn.execute(
                "update subscription_periods set plan_id = $2, traffic_limit_bytes = $3, "
                "period_end = period_start + make_interval(days => $4) where id = $1",
                current.id,
                plan_id,
                plan["traffic_limit_bytes"],
                plan["duration_days"],
            )
            await conn.execute(
                "update subscriptions set device_limit = $2 where user_id = $1",
                user_id,
                plan["device_limit"],
            )
        await self._composition.publish_user(user_id)

    async def add_traffic(
        self,
        user_id: uuid.UUID,
        bytes: int,  # noqa: A002 — имя параметра из описания задачи 001.21
        actor: uuid.UUID | None,
        reason: str | None,
        *,
        ref_key: str | None = None,
    ) -> None:
        """Начислить трафик текущему периоду: бонус Redeem- или Promo-кода (§4.14) либо
        увеличение лимита администратором (§4.11 «Восстановление доступа»). Пишется отрицательной
        дельтой в ``balance_entries`` и уменьшает ``used_billable_bytes``, отчего остаток растёт
        ровно на начисленное (выбор объяснён в докстринге модуля).

        ``actor`` задан — это корректировка администратора (``adjustment``), и ``reason``
        обязателен: ``CHECK (source <> 'adjustment' OR reason IS NOT NULL)`` §4.2.4. ``actor``
        пуст — погашение кода пользователем (``bonus``). ``ref_key`` — чем вызвано начисление
        (идентификатор кода у 001.20); без него заводится идентификатор самой корректировки:
        колонка ``NOT NULL``, и заполнить её иначе нечем.

        Начисление возвращает доступ исчерпавшему лимит: при положительном остатке состояние
        ``suspended_quota`` снимается в ``active`` (§4.11 «Восстановление доступа»). Блокировку
        администратора (``suspended_admin``) начисление не снимает — её снимает администратор."""
        if bytes <= 0:
            raise ApiError("invalid_amount", "начисление должно быть положительным", status=422)
        source: BalanceSource = "adjustment" if actor is not None else "bonus"
        if source == "adjustment" and not reason:
            raise ApiError("reason_required", "корректировке нужна причина", status=422)
        async with transaction(self._pool) as conn:
            current = await self._current(conn, user_id, lock=True)
            if current is None:
                raise _no_subscription()
            await conn.execute(
                "insert into balance_entries (period_id, source, delta_billable_bytes, ref_key, "
                "actor_id, reason) values ($1, $2, $3, $4, $5, $6)",
                current.id,
                source,
                -bytes,
                ref_key or str(uuid.uuid4()),
                actor,
                reason,
            )
            used: int = await conn.fetchval(
                "update subscription_periods set used_billable_bytes = used_billable_bytes - $2 "
                "where id = $1 returning used_billable_bytes",
                current.id,
                bytes,
            )
            left = remaining_of(current.traffic_limit_bytes, used)
            if current.state == "suspended_quota" and (left is None or left > 0):
                await self._set_state(conn, user_id, "active")
        await self._composition.publish_user(user_id)

    async def expire(self, user_id: uuid.UUID) -> None:
        """Перевести подписку в ``expired`` по истечении периода: пользователь удаляется из
        inbound на всех нодах, обменивающихся с Control Plane, в пределах Н-14 (§4.11 «Состояние
        пользователя на ноде»). Идемпотентно: планировщик 001.83 ходит раз в минуту и может
        принести одного пользователя дважды — повторный вызов состояние не трогает и состав не
        будит, иначе поток просыпался бы каждую минуту на каждого истёкшего."""
        async with transaction(self._pool) as conn:
            changed = await conn.fetchval(
                "update subscriptions set state = 'expired', state_changed_at = now() "
                "where user_id = $1 and state <> 'expired' returning true",
                user_id,
            )
        if changed:
            await self._composition.publish_user(user_id)

    async def set_state(self, user_id: uuid.UUID, state: SubscriptionState, reason: str) -> None:
        """Установить состояние подписки с причиной: ``suspended_quota`` при исчерпании лимита
        (§4.11 «Действие при исчерпании» — переводит ``LimitsService``, 001.35),
        ``suspended_admin`` — блокировка администратором, ``active`` — восстановление. Колонки
        причины у ``subscriptions`` нет (только ``state`` и ``state_changed_at``): причина уходит
        в Audit Log (§4.16) и в поле ``reason`` ответа кабинета (§4.2, UC-16 A1)."""
        async with transaction(self._pool) as conn:
            if not await conn.fetchval(
                "select true from subscriptions where user_id = $1", user_id
            ):
                raise _no_subscription()
            await self._set_state(conn, user_id, state)
        await self._composition.publish_user(user_id)

    async def remaining(self, user_id: uuid.UUID) -> int | None:
        """Остаток лимита текущего периода в единицах списания: лимит минус израсходованное,
        но не меньше нуля — разность бывает отрицательной, потому что списание за перерасход
        ограничено Н-17б (``Н-17 × traffic_multiplier`` ноды), а ``remaining`` кабинета и
        статистики объявлены ``ge=0``. Перерасход ноды без связи ограничен не этим, а грантом
        Н-17в. ``None`` — это Unlimited (``traffic_limit_bytes`` NULL, §4.11 «Типы лимитов»);
        без подписки — тоже ``None``, и отличает эти два случая состояние (``none`` против
        ``active``), которое кабинет показывает рядом. Чтение состав не публикует."""
        async with self._pool.acquire() as conn:
            current = await self._current(conn, user_id, lock=False)
        if current is None:
            return None
        return remaining_of(current.traffic_limit_bytes, current.used_billable_bytes)

    # --- общее ---------------------------------------------------------------------------------

    @staticmethod
    async def _plan(conn: asyncpg.Connection, plan_id: uuid.UUID) -> asyncpg.Record:
        """Тариф, по которому заводится период. Архивный не годится: UC-09 A4 переводит в
        ``archived`` тариф, снятый с выдачи, и новая подписка по нему обошла бы это решение;
        уже выданные подписки архивный статус не трогает."""
        plan = await conn.fetchrow(
            "select duration_days, traffic_limit_bytes, device_limit, status from plans "
            "where id = $1",
            plan_id,
        )
        if plan is None:
            raise ApiError("unknown_plan", "тариф не найден", status=422)
        if plan["status"] == "archived":
            raise ApiError("plan_archived", "тариф снят с выдачи", status=409)
        return plan

    @staticmethod
    async def _current(
        conn: asyncpg.Connection, user_id: uuid.UUID, *, lock: bool
    ) -> Period | None:
        """Текущий период и состояние подписки; ``None`` — подписки или периода нет.

        ``lock`` — блокировка строки ``subscriptions`` на время перехода: два одновременных
        продления иначе завели бы два периода, и каждое объявило бы текущим свой, а инвариант
        «один активный период на пользователя» (§4.2.4) держится не ограничением базы, а
        доменом."""
        row = await conn.fetchrow(_CURRENT_LOCKED if lock else _CURRENT, user_id)
        if row is None or row["id"] is None:
            return None
        return Period(
            id=row["id"],
            plan_id=row["plan_id"],
            state=row["state"],
            period_start=row["period_start"],
            period_end=row["period_end"],
            traffic_limit_bytes=row["traffic_limit_bytes"],
            used_billable_bytes=row["used_billable_bytes"],
        )

    @staticmethod
    async def _open_period(
        conn: asyncpg.Connection,
        user_id: uuid.UUID,
        plan_id: uuid.UUID,
        plan: asyncpg.Record,
        source: PeriodSource,
        source_id: uuid.UUID | None,
        *,
        at: dt.datetime | None,
    ) -> PeriodId:
        """Завести период тарифа от ``at`` (``None`` — от текущего момента базы) на срок тарифа.
        Израсходованное новое — ноль по умолчанию колонки: остаток прежнего периода не
        переносится (§4.11). Начало и конец считаются одним оператором, чтобы срок мерился от
        одной точки."""
        period_id: PeriodId = await conn.fetchval(
            "insert into subscription_periods (user_id, plan_id, period_start, period_end, "
            "traffic_limit_bytes, source, source_id) "
            "select $1, $2, s.start, s.start + make_interval(days => $4), $5, $6, $7 "
            "from (select coalesce($3::timestamptz, now()) as start) s returning id",
            user_id,
            plan_id,
            at,
            plan["duration_days"],
            plan["traffic_limit_bytes"],
            source,
            source_id,
        )
        return period_id

    @staticmethod
    async def _set_current(
        conn: asyncpg.Connection,
        user_id: uuid.UUID,
        period_id: PeriodId,
        state: SubscriptionState,
        device_limit: int | None,
    ) -> None:
        """Строка состояния одна на пользователя (первичный ключ — ``user_id``), поэтому запись
        — upsert. ``device_limit`` — снимок из тарифа (§4.2.4): тариф потом изменят, а в этом
        периоде обязан действовать тот, по которому подписку выдали."""
        await conn.execute(
            "insert into subscriptions (user_id, state, current_period_id, device_limit, "
            "state_changed_at) values ($1, $2, $3, $4, now()) on conflict (user_id) do update "
            "set state = excluded.state, current_period_id = excluded.current_period_id, "
            "device_limit = excluded.device_limit, state_changed_at = now()",
            user_id,
            state,
            period_id,
            device_limit,
        )

    @staticmethod
    async def _set_state(
        conn: asyncpg.Connection, user_id: uuid.UUID, state: SubscriptionState
    ) -> None:
        await conn.execute(
            "update subscriptions set state = $2, state_changed_at = now() where user_id = $1",
            user_id,
            state,
        )
