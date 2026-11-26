"""События уведомлений §4.17 (задача 001.51; R-39, R-40; data-model.md §4.2.6 ``events``).

``EventType`` — двенадцать типов таблицы «События» §4.17 постановки, значения — метки
перечисления PostgreSQL ``event_type`` (миграция 0001). ``emit`` записывает событие в ``events``
на подключении транзакции вызывающего (outbox, interfaces.md §5.4) с ключом подавления повторов
``dedup_key`` и для новой строки ставит задачи доставки в очередь ``background``:
``notify.email`` (ключ ``notify:email:{event_id}``) и ``notify.webhook`` на каждого получателя
``settings.webhooks`` (ключ ``notify:webhook:{event_id}:{sha256(url)}``: один ключ на событие дал
бы одну задачу — ``enqueue`` вернул бы уже стоящую; адрес — отпечатком, потому что строка индекса
ключей задач ограничена 2704 байтами, и длинный адрес сорвал бы операцию вызывающего). Выбор
каналов по таблице §4.17 и язык получателя — 001.52; до неё почта и webhook ставятся на каждое
событие.

``dedup_key(type, **части)`` — формулы ключа по типам (решения заказчика 2026-10-25, В-7, В-41,
и 2026-11-26; уточнения задачи 2026-11-26):

- события подписки («активирована», «истекает», «истекла») —
  ``{type}:{user_id}:{period_id}:{period_end}``: продление и смена тарифа, сдвинувшие конец
  периода, дают новое событие, и предупреждение приходит перед новым концом, а не перед прежним
  (решение заказчика 2026-11-26);
- лимиты — ``{type}:{user_id}:{period_id}``; ``traffic_exhausted`` — ещё момент исчерпания:
  каждый новый отзыв доступа в периоде даёт новое событие, 80 и 95 % — одно за период;
- ``node_address_changed`` — строка на каждого пользователя ноды,
  ``{type}:{user_id}:{node_id}:{kind}:{version}``: вид ``address`` или ``params`` входит в ключ,
  потому что версия — числа двух рядов (``id`` строки ``node_ip_history`` и версия конфигурации
  Xray);
- операторские — ``{type}:{node_id}:{эпизод}``: момент перехода статуса ноды для «недоступна»,
  «восстановлена», «приостановлена провайдером»; ``period_end`` последнего принятого интервала
  для ``node_report_buffer_full``; для ``reconciliation_mismatch`` —
  ``{type}:{node_id}:{kind}:{эпизод}``: сутки у межисточниковой сверки, начало разрыва
  (``gap_start``) у сверки непрерывности — каждый новый разрыв даёт своё событие; арифметическая
  сверка идёт по всей системе — ``{type}:arithmetic:{сутки}``, без ноды (решения заказчика
  2026-11-26).

Повтор в эпизоде подавляется, новый эпизод даёт новое событие. ``dedup_key`` защищает от
повторной вставки и доставки при повторе операции; правилом порога он не является —
однократность 80 % и 95 % за период держат колонки периода (001.35): строка ``events`` хранится
90 дней (data-model §4.5).

``emit`` сверяет ключ с событием до записи: тип, адресат (``user_id``, ``node_id``) и вид
(``payload.kind`` у ``node_address_changed`` и ``reconciliation_mismatch``) — ключ чужого
пользователя или чужого вида иначе подавил бы его настоящее событие.

Битый элемент ``settings.webhooks`` (не объект со строкой ``url``, повтор адреса) пропускается с
ERROR в журнале: уведомление — побочный канал, операция вызывающего из-за настройки не
откатывается (решение заказчика 2026-11-26). Сид ключа — 001.79; до него получателей нет.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

import asyncpg

from app.jobs.queue import enqueue
from app.notifications.delivery import NOTIFY_EMAIL, NOTIFY_WEBHOOK, QUEUE

log = logging.getLogger(__name__)

# Типы, под которыми emit ставит задачи; тесты подменяют их (``tests/conftest.py``), чтобы задачи
# под тестом не забрал живой исполнитель стенда. Боевые значения сверяет
# ``tests/unit/notifications/test_events.py`` на копии модуля без подмены.
EMAIL_JOB = NOTIFY_EMAIL
WEBHOOK_JOB = NOTIFY_WEBHOOK


class EventType(enum.StrEnum):
    """Двенадцать событий §4.17 в порядке таблицы постановки."""

    SUBSCRIPTION_ACTIVATED = "subscription_activated"
    SUBSCRIPTION_EXPIRING = "subscription_expiring"
    SUBSCRIPTION_EXPIRED = "subscription_expired"
    TRAFFIC_80 = "traffic_80"
    TRAFFIC_95 = "traffic_95"
    TRAFFIC_EXHAUSTED = "traffic_exhausted"
    NODE_ADDRESS_CHANGED = "node_address_changed"
    NODE_OFFLINE = "node_offline"
    NODE_RECOVERED = "node_recovered"
    NODE_SUSPENDED_BY_PROVIDER = "node_suspended_by_provider"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    NODE_REPORT_BUFFER_FULL = "node_report_buffer_full"


# Адресат — пользователь (семь событий) или оператор (пять), §4.17.
USER_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.SUBSCRIPTION_ACTIVATED,
        EventType.SUBSCRIPTION_EXPIRING,
        EventType.SUBSCRIPTION_EXPIRED,
        EventType.TRAFFIC_80,
        EventType.TRAFFIC_95,
        EventType.TRAFFIC_EXHAUSTED,
        EventType.NODE_ADDRESS_CHANGED,
    }
)
OPERATOR_EVENTS: frozenset[EventType] = frozenset(EventType) - USER_EVENTS

ADDRESS_CHANGE_KINDS = frozenset({"address", "params"})  # payload.kind, В-41
ARITHMETIC = "arithmetic"
CONTINUITY = "continuity"
# reconciliation_kind, data-model §4.2.5; payload.kind события сверки (001.37d).
RECONCILIATION_KINDS = frozenset({ARITHMETIC, "cross_source", CONTINUITY})
# Виды по типу: часть ключа ``kind`` и ``payload.kind`` события.
KINDS: dict[EventType, frozenset[str]] = {
    EventType.NODE_ADDRESS_CHANGED: ADDRESS_CHANGE_KINDS,
    EventType.RECONCILIATION_MISMATCH: RECONCILIATION_KINDS,
}

_USER_PERIOD = ("user_id", "period_id")
_SUBSCRIPTION = (*_USER_PERIOD, "period_end")  # конец периода — эпизод (2026-11-26)
_NODE_STATUS = ("node_id", "status_changed_at")
# Части ключа по типу, в порядке ключа; у сверки — по виду (``parts_of``): межисточниковая —
# эта строка, арифметическая — ``ARITHMETIC_PARTS``, непрерывность — ``CONTINUITY_PARTS``.
PARTS: dict[EventType, tuple[str, ...]] = {
    EventType.SUBSCRIPTION_ACTIVATED: _SUBSCRIPTION,
    EventType.SUBSCRIPTION_EXPIRING: _SUBSCRIPTION,
    EventType.SUBSCRIPTION_EXPIRED: _SUBSCRIPTION,
    EventType.TRAFFIC_80: _USER_PERIOD,
    EventType.TRAFFIC_95: _USER_PERIOD,
    EventType.TRAFFIC_EXHAUSTED: (*_USER_PERIOD, "exhausted_at"),
    EventType.NODE_ADDRESS_CHANGED: ("user_id", "node_id", "kind", "version"),
    EventType.NODE_OFFLINE: _NODE_STATUS,
    EventType.NODE_RECOVERED: _NODE_STATUS,
    EventType.NODE_SUSPENDED_BY_PROVIDER: _NODE_STATUS,
    EventType.RECONCILIATION_MISMATCH: ("node_id", "kind", "day"),
    EventType.NODE_REPORT_BUFFER_FULL: ("node_id", "period_end"),
}
CONTINUITY_PARTS = ("node_id", "kind", "gap_start")  # каждый разрыв — своё событие (2026-11-26)
# Арифметическая сверка — по всей системе за сутки, без ноды (001.33 ``arithmetic(conn, day)``;
# решение заказчика 2026-11-26).
ARITHMETIC_PARTS = ("kind", "day")

_IDS = frozenset({"user_id", "node_id", "period_id"})
_MOMENTS = frozenset({"exhausted_at", "status_changed_at", "period_end", "gap_start"})
# Адресат по типу: пользовательским нужен пользователь, операторским — нода, смене адреса — оба.
_SUBJECT: dict[EventType, tuple[bool, bool]] = {
    event: (
        event in USER_EVENTS,
        event not in USER_EVENTS or event is EventType.NODE_ADDRESS_CHANGED,
    )
    for event in EventType
}
_INSERT = (
    "insert into events (type, user_id, node_id, payload, dedup_key) "
    "values ($1::event_type, $2, $3, $4::jsonb, $5) "
    "on conflict (dedup_key) do nothing returning id"
)


def _event(type_: EventType | str) -> EventType:
    try:
        return EventType(type_)
    except ValueError:
        raise ValueError(f"тип события {type_!r} — не из 12 типов §4.17") from None


def _uuid(event: EventType, name: str, value: Any) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        raise ValueError(f"{event.value}: {name} {value!r} — не UUID") from None


def _part(event: EventType, name: str, value: Any) -> str:
    """Часть ключа строкой; негодное значение — ``ValueError`` с типом и именем части."""
    where = f"{event.value}: часть {name}"
    if name in _IDS:
        return str(_uuid(event, name, value))
    if name in _MOMENTS:
        if not isinstance(value, dt.datetime) or value.tzinfo is None:
            raise ValueError(f"{where}: нужен момент с часовым поясом, получено {value!r}")
        return value.astimezone(dt.UTC).isoformat(timespec="microseconds")
    if name == "day":
        if not isinstance(value, dt.date) or isinstance(value, dt.datetime):
            raise ValueError(f"{where}: нужен день (date), получено {value!r}")
        return value.isoformat()
    if name == "kind":
        if value not in KINDS[event]:
            raise ValueError(f"{where}: {value!r} — не из {sorted(KINDS[event])}")
        return str(value)
    # version: id строки node_ip_history или версия конфигурации Xray — целое не меньше 0.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where}: нужно целое не меньше 0, получено {value!r}")
    return str(value)


def parts_of(type: EventType | str, kind: str | None = None) -> tuple[str, ...]:  # noqa: A002
    """Части ключа события ``type``; у сверки — по виду ``kind``: непрерывность — начало разрыва
    вместо суток, арифметическая — без ноды."""
    event = _event(type)
    if event is EventType.RECONCILIATION_MISMATCH and kind == CONTINUITY:
        return CONTINUITY_PARTS
    if event is EventType.RECONCILIATION_MISMATCH and kind == ARITHMETIC:
        return ARITHMETIC_PARTS
    return PARTS[event]


def dedup_key(type: EventType | str, **parts: Any) -> str:  # noqa: A002 — имя контракта задачи
    """Ключ подавления повторов события ``type`` из его частей (``parts_of``): ``{type}:…``.

    Недостающая (или ``None``), лишняя и негодная часть — ``ValueError``: иначе ключ молча совпал
    бы с ключом другого события или разошёлся бы с ключом того же. Моменты — UTC в ISO 8601 с
    микросекундами, поэтому один момент в любом часовом поясе даёт один ключ.
    """
    event = _event(type)
    names = parts_of(event, parts.get("kind"))
    missing = [n for n in names if parts.get(n) is None]
    extra = sorted(set(parts) - set(names))
    if missing or extra:
        raise ValueError(
            f"{event.value}: части ключа — {list(names)}; нет {missing}, лишние {extra}"
        )
    return ":".join([event.value, *(_part(event, n, parts[n]) for n in names)])


def _subject_prefix(
    event: EventType,
    user_id: uuid.UUID | None,
    node_id: uuid.UUID | None,
    payload: Mapping[str, Any],
) -> str:
    """Начало ключа, которое задают адресат и вид события: ключ чужого адресата или вида
    подавил бы его настоящее событие. Арифметическая сверка — без ноды (решение заказчика
    2026-11-26)."""
    kind = payload.get("kind") if event in KINDS else None
    if event in KINDS and kind not in KINDS[event]:
        raise ValueError(f"{event.value}: payload.kind {kind!r} — не из {sorted(KINDS[event])}")
    needs_user, needs_node = _SUBJECT[event]
    if event is EventType.RECONCILIATION_MISMATCH and kind == ARITHMETIC:
        needs_node = False
    if (user_id is not None) != needs_user or (node_id is not None) != needs_node:
        raise ValueError(
            f"{event.value}: адресат — {'user_id' if needs_user else 'без user_id'}, "
            f"{'node_id' if needs_node else 'без node_id'}; получено user_id={user_id}, "
            f"node_id={node_id}"
        )
    segments = [event.value]
    if user_id is not None:
        segments.append(str(_uuid(event, "user_id", user_id)))
    if node_id is not None:
        segments.append(str(_uuid(event, "node_id", node_id)))
    if kind is not None:
        segments.append(str(kind))
    return ":".join(segments) + ":"


def _jsonable(value: Any) -> Any:
    """Значения нагрузки, которые ``json`` не знает: день и момент — ISO 8601, число
    ``numeric`` — строкой (точно), UUID — строкой."""
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, decimal.Decimal | uuid.UUID):
        return str(value)
    raise TypeError(f"значение нагрузки события {value!r} ({type(value).__name__}) — не JSON")


def _jsonb_refusal(value: Any) -> str | None:
    """Чего ``jsonb`` не примет в строке нагрузки (ключе или значении): символ NUL и одиночный
    суррогат Unicode (не кодируется в UTF-8); ``None`` — всё принимается."""
    if isinstance(value, str):
        if "\x00" in value:
            return "символ NUL в строке"
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return "одиночный суррогат Unicode в строке"
        return None
    if isinstance(value, dict):
        items: list[Any] = [part for pair in value.items() for part in pair]
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        return None
    for item in items:
        if reason := _jsonb_refusal(item):
            return reason
    return None


def _document(body: Mapping[str, Any]) -> str:
    """Нагрузка события в JSON для ``jsonb``. Чего ``jsonb`` не примет — NaN, бесконечность,
    символ NUL и одиночный суррогат в строке — ``TypeError`` до записи: иначе отказ PostgreSQL
    сорвал бы транзакцию вызывающего."""
    try:
        document = json.dumps(body, default=_jsonable, allow_nan=False)
    except ValueError as exc:  # NaN и бесконечность: allow_nan=False
        raise TypeError(f"нагрузка события — не JSON: {exc}") from exc
    if reason := _jsonb_refusal(dict(body)):
        raise TypeError(f"нагрузка события — не JSON для jsonb: {reason}")
    return document


async def emit(
    conn: asyncpg.Connection,
    type: EventType | str,  # noqa: A002 — имя контракта задачи
    user_id: uuid.UUID | None = None,
    node_id: uuid.UUID | None = None,
    payload: Mapping[str, Any] | None = None,
    *,
    dedup_key: str,
) -> uuid.UUID | None:
    """Записать событие в ``events`` и поставить задачи доставки; вернуть ``id`` новой строки,
    ``None`` — повтор с тем же ``dedup_key`` подавлен (ни строки, ни задач).

    Идёт на подключении транзакции вызывающего (иначе ``RuntimeError`` до записи): событие и
    задачи коммитятся и откатываются вместе с операцией (outbox §5.4). Второй ``emit`` с тем же
    ключом в соседней транзакции ждёт её исхода на уникальном индексе: при READ COMMITTED коммит
    первой — подавление, откат — событие пишет второй; при REPEATABLE READ и SERIALIZABLE коммит
    первой даёт второй ошибку сериализации, и вызывающий повторяет операцию (обычное правило этих
    уровней). Адресат по типу, начало ключа (тип, адресат, вид) и ``payload.kind`` событий с видом
    проверяются до записи — ``ValueError``; нагрузка не JSON — ``TypeError`` до записи.
    """
    event = _event(type)
    if not conn.is_in_transaction():
        raise RuntimeError(f"{event.value}: emit идёт в транзакции вызывающего (outbox §5.4)")
    body = dict(payload or {})
    prefix = _subject_prefix(event, user_id, node_id, body)
    if not dedup_key.startswith(prefix) or len(dedup_key) == len(prefix):
        raise ValueError(
            f"{event.value}: ключ {dedup_key!r} — не ключ этого события (ждём {prefix}…, dedup_key)"
        )
    document = _document(body)
    event_id: uuid.UUID | None = await conn.fetchval(
        _INSERT, event.value, user_id, node_id, document, dedup_key
    )
    if event_id is None:
        return None
    await enqueue(conn, QUEUE, EMAIL_JOB, {"event_id": str(event_id)}, f"notify:email:{event_id}")
    for url in await webhook_urls(conn):
        await enqueue(
            conn,
            QUEUE,
            WEBHOOK_JOB,
            {"event_id": str(event_id), "url": url},
            webhook_job_key(event_id, url),
        )
    return event_id


def webhook_job_key(event_id: uuid.UUID, url: str) -> str:
    """Ключ задачи доставки webhook: событие и отпечаток адреса получателя (SHA-256)."""
    return f"notify:webhook:{event_id}:{hashlib.sha256(url.encode('utf-8')).hexdigest()}"


async def webhook_urls(conn: asyncpg.Connection) -> list[str]:
    """Адреса получателей ``settings.webhooks`` (элемент ``{url, secret}``, сид — 001.79) в
    порядке списка. Ключа нет — получателей нет. Элемент не объект со строкой ``url`` или повтор
    адреса (секрет подписи 001.79 ищет по адресу) и значение не списком — ERROR в журнале и
    пропуск: операция вызывающего из-за настройки не откатывается (решение заказчика
    2026-11-26)."""
    raw = await conn.fetchval("select value from settings where key = 'webhooks'")
    if raw is None:
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(value, list):
        log.error("settings.webhooks: значение не список получателей — webhook не ставится")
        return []
    urls: list[str] = []
    for number, item in enumerate(value):
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or not url:
            log.error("settings.webhooks[%d]: нет строки url — получатель пропущен", number)
        elif url in urls:
            log.error("settings.webhooks[%d]: повтор адреса — получатель пропущен", number)
        else:
            urls.append(url)
    return urls
