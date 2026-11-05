"""Потоки конфигурации и состава для ноды (interfaces.md §5.2; постановка §4.6 «Матрица влияния»;
data-model.md §4.2.3 ``node_config_versions``, ``node_user_state``, ``node_user_credentials``;
UC-01, UC-07, UC-11).

Задача 001.28: схемы ответа состояния и ``CompositionService`` с фиксированными значениями.

Задача 001.29 — поток состава на строках базы: номер изменения выделяет ``next_users_seq``
(``UPDATE nodes … RETURNING`` под блокировкой строки ноды); исполнитель ``composition.publish_user``
пишет строки ``node_user_state`` и credentials пар по пересечению групп доступа тарифа и ноды;
выдача читает курсор ноды, её ``resync_required`` и строки в одном снимке базы, применяет вентиль,
взводит ``resync_required`` в колонке ноды у урезанного ответа и расшифровывает credentials только
строк ноды из identity. Постановка публикации в транзакции бизнес-операции, ``publish_node`` и
``remove_user`` — 001.29b; поток конфигурации — 001.29c; поколение и контрольная сумма состава —
001.75b; удержание long-poll до 30 с на канале Redis ``node:{id}`` — 001.75c; подтверждение
курсоров и снятие ``resync_required`` — 001.75; конфигурация Xray и её версии — 001.26, 001.27.

Задача 001.26: раздел ``agent`` ответа — параметры агента вне документа Xray (лимиты nftables,
коэффициент, стратегия разрыва) со своим курсором версии. Раздел не входит в ``config.json`` и в
``config.checksum``: его смена применяется агентом без перезапуска Xray (перезагрузка набора
nftables, запись в хранилище агента), а перезапуск — только при смене контрольной суммы
документа (interfaces.md §5.2). Значения заполняют 001.27 (лимиты), 001.36 (коэффициент), 001.40
(стратегия).

Что остаётся заглушкой: поток конфигурации (каркас ``STUB_CONFIG_DOCUMENT`` версии 1), раздел
``agent`` версии 1 и поколение 1 — «что нового» по ним выводится из курсоров запроса. Вентиль по
статусу берётся из ``domain.statuses`` (§4.6 — единственный источник правил), тег пользователя
выводится из его идентификатора (``xray_email``), а поток конфигурации не несёт credentials —
инвариант §5.2.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from cryptography.exceptions import InvalidTag
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from redis.exceptions import RedisError

from app.domain.commands import Command, CommandService
from app.domain.multiplier import MULTIPLIER_MILLI_MAX, MULTIPLIER_STEP_MILLI
from app.domain.nodes import Node
from app.domain.statuses import INT4_MAX, INT8_MAX, STREAM_GATE
from app.errors import ApiError
from app.redis import get_redis
from app.security.crypto import Binding, CipherMismatch, FieldCipher, bound_to, checked_cipher

log = logging.getLogger(__name__)

# Перечисление ``user_node_state`` data-model.md §4.2.3.
UserState = Literal["active", "suspended_quota", "suspended_admin", "expired", "removed"]
# «Только отзывы» §4.6: изменения, снимающие доступ. Всё, кроме ``active``, доступ снимает.
REVOKING_STATES: frozenset[UserState] = frozenset(
    ("suspended_quota", "suspended_admin", "expired", "removed")
)

Checksum = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# Стратегии разрыва соединений пользователя — кандидаты PoC-1а (001.88, ОВ-22): удаление и
# повторное добавление пользователя (``readd``) или правило ``RoutingService`` по адресам
# (``route_block``). Перезапуск inbound только замеряется и выбран быть не может (В-9) — в
# перечислении его нет; после выбора в перечислении останется одна стратегия (001.40, В-11).
DisconnectStrategy = Literal["readd", "route_block"]


def xray_email(user_id: uuid.UUID) -> str:
    """Устойчивый тег пользователя в Xray (§4.2.3 ``node_user_credentials.xray_email``, §5.9):
    ``u`` и идентификатор. Тег переживает ротацию credentials — по нему сводятся счётчики."""
    return f"u{user_id}"


def config_checksum(document: Mapping[str, Any]) -> str:
    """SHA-256 канонической записи конфигурации: ключи отсортированы, разделители без пробелов,
    не-ASCII символы не экранируются, кодировка UTF-8. Нода считает сумму тем же правилом и
    сверяет её с полем ``checksum`` — сериализация ответа при этом может отличаться."""
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class StateConfig(BaseModel):
    """Поток конфигурации: полная конфигурация Xray без credentials пользователей (§5.2), её
    версия ``node_config_versions.config_version`` и контрольная сумма."""

    version: int = Field(ge=1)
    checksum: Checksum
    document: dict[str, Any] = Field(
        alias="json", description="конфигурация Xray целиком, без credentials пользователей"
    )


class StateAgent(BaseModel):
    """Раздел ``agent`` ответа состояния: параметры агента вне документа Xray (§5.2).

    ``version`` — свой курсор раздела, независимый от ``config_version``: смена лимита или
    коэффициента выпускает новую версию раздела, а не новую версию конфигурации, и Xray не
    перезапускается (иначе каждая смена параметра рвала бы соединения всех пользователей ноды и
    открывала новую эпоху счётчиков). Применённую версию агент присылает параметром
    ``agent_section_version`` запроса состояния."""

    version: int = Field(ge=1, le=INT4_MAX)
    max_conn_per_ip: int = Field(
        ge=1, le=INT4_MAX, description="одновременных соединений с одного адреса источника, Н-29"
    )
    new_conn_rate_per_ip: int = Field(
        ge=1,
        le=INT4_MAX,
        description="новых соединений в секунду с одного адреса клиента на порты inbound (В-10)",
    )
    multiplier_milli: int = Field(
        ge=0,
        le=MULTIPLIER_MILLI_MAX,
        multiple_of=MULTIPLIER_STEP_MILLI,
        description="коэффициент трафика ноды в тысячных (шаг 0.1): расход гранта в единицах "
        "списания",
    )
    disconnect_strategy: DisconnectStrategy = Field(
        description="стратегия разрыва соединений при отзыве и исчерпании гранта (PoC-1а)"
    )


class Credentials(BaseModel):
    """Credentials пары «пользователь × нода» (``node_user_credentials``), расшифрованные C-01
    для ноды, определённой по identity запроса (§5.2): строки выбирает ``CompositionService._read``
    по ``node_id`` этой ноды, расшифровывает ``_row``.

    Тега здесь нет: он о пользователе, а не о паре, и нужен как раз строкам без ключей —
    поэтому живёт в ``UserRow``."""

    vless_uuid: uuid.UUID
    trojan_password: str
    version: int = Field(ge=1, description="версия credentials, растёт при ротации Н-32")


class UserRow(BaseModel):
    """Строка потока состава (``node_user_state``): состояние доступа, устойчивый тег, остаток
    гранта квоты, заблокированные адреса (R-27) и номер изменения.

    Credentials несёт только строка, которая доступ **выдаёт** (``active``): ключ нужен, чтобы
    добавить пользователя в inbound. Строке, снимающей доступ, он не нужен — удалять и
    блокировать нода умеет по тегу ``xray_email``, который есть у **каждой** строки, — и потому
    не выдаётся: отфильтрованный поток
    ``disabled`` и ``suspended`` (§4.6 «Только отзывы») состоит из одних таких строк, а это
    статусы «отключена администратором» и «приостановлена провайдером по жалобе» (Р-3), где
    везти на ноду живые ключи пар «пользователь × нода» незачем. Восстановление доступа приходит
    новой строкой ``active`` — с ключом."""

    user_id: uuid.UUID
    state: UserState
    xray_email: str = Field(description="устойчивый тег §4.2.3: им нода применяет любую строку")
    quota_grant_bytes: int = Field(ge=0)
    blocked_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
    updated_seq: int = Field(ge=1)
    credentials: Credentials | None = None

    @model_validator(mode="after")
    def _keys_only_where_access_is_granted(self) -> UserRow:
        """Инвариант держится моделью, а не подбором строк: выдача собирает строки JOIN
        ``node_user_state × node_user_credentials`` (001.29), запись credentials у снятой пары
        есть, и джойн, вернувший ключ на приостановленную строку, молча отправил бы живые ключи
        на отключённую ноду."""
        if (self.credentials is not None) is not (self.state == "active"):
            raise ValueError("ключи несёт ровно строка, выдающая доступ")
        return self


class Users(BaseModel):
    """Поток состава: новый курсор ноды, признак полноты и строки.

    ``seq`` — наибольший ``updated_seq`` выборки **до** фильтра по статусу: строки, скрытые
    вентилем, нода добирает снапшотом после выхода из фильтрующего статуса, а не повторной
    дельтой (§5.2). При пустой выборке курсор не двигается и равен присланному; у снапшота
    ``seq`` — текущий номер изменения, а не наибольший среди выданных строк.

    ``full`` — состав описан целиком: применяя такой ответ, нода удаляет пользователей, которых
    в нём нет (§4.2.3). Ответ, урезанный вентилем по статусу или пределом размера, приходит с
    ``full = false``, даже если нода просила снапшот, — иначе она вычистила бы у себя всех, кого
    скрыл вентиль."""

    seq: int = Field(ge=0)
    full: bool = Field(description="состав описан целиком: отсутствующих нода удаляет")
    rows: list[UserRow]


class StateResponse(BaseModel):
    """Ответ ``GET /agent/v1/state``: дельты обоих потоков, раздел параметров агента, поколение
    identity, признак обязательного снапшота и команды канала (§5.2)."""

    config: StateConfig | None = None
    agent: StateAgent | None = None
    users: Users
    generation: int = Field(ge=1)
    resync_required: bool
    commands: list[Command]


class AckIn(BaseModel):
    """Тело ``POST /agent/v1/ack`` (§5.2): курсоры, которые нода применила и удержит после
    перезапуска."""

    model_config = ConfigDict(extra="forbid")

    applied_config_version: int = Field(ge=0, le=INT4_MAX)
    applied_users_seq: int = Field(ge=0, le=INT8_MAX)


# --- фиксированные значения заглушки ------------------------------------------------------------

STUB_CONFIG_VERSION = 1
STUB_AGENT_VERSION = 1
STUB_GENERATION = 1  # первое поколение identity: ``node_identities.generation`` начинается с 1
# Размер гранта заглушки — 1 ГиБ; ту же константу использует служба грантов (``accounting.quota``
# импортирует её отсюда: домен не импортирует учёт). Настоящий размер задаёт 001.36 (R-26,
# ОВ-A7), и в поток состава он придёт через граф зависимостей, а не импортом.
STUB_QUOTA_GRANT_BYTES = 1073741824

# Каркас конфигурации Xray: структура без пользователей. Это не выдача генератора: форму
# серверного документа в именах v26.10.31 объявляет ``XrayConfigBuilder`` (заглушка 001.26,
# сборка 001.27), а поток конфигурации начнёт отдавать его документ из ``node_config_versions``
# с 001.29c. Здесь важно одно свойство §5.2: состав inbound пуст, credentials пользователей
# потоком конфигурации не передаются.
STUB_CONFIG_DOCUMENT: dict[str, Any] = {
    "log": {"loglevel": "warning"},
    "inbounds": [
        {
            "tag": "vless_raw_vision",
            "port": 443,
            "protocol": "vless",
            "settings": {"clients": [], "decryption": "none"},
            "streamSettings": {"network": "raw", "security": "reality"},
        }
    ],
    "outbounds": [
        {"tag": "direct", "protocol": "freedom"},
        {"tag": "blackhole", "protocol": "blackhole"},
    ],
}
# Сумма постоянна для пары «нода × версия конфигурации»: в бою она лежит в строке
# ``node_config_versions`` и считается один раз при публикации (001.27), а не при каждой выдаче.
STUB_CONFIG_CHECKSUM = config_checksum(STUB_CONFIG_DOCUMENT)

# Раздел ``agent`` заглушки. ``max_conn_per_ip`` равен лимиту карточки ``stub_node``;
# ``new_conn_rate_per_ip`` — значение заглушки, не умолчание: умолчания у поля нет, оно
# обязательно (В-10); коэффициент 1.0; стратегия — рекомендованная задачей PoC-1а, выбор — 001.88.
STUB_AGENT = StateAgent(
    version=STUB_AGENT_VERSION,
    max_conn_per_ip=32,
    new_conn_rate_per_ip=16,
    multiplier_milli=1000,
    disconnect_strategy="readd",
)


def stub_agent() -> StateAgent:
    """Раздел ``agent`` заглушки: фиксированные значения (копия — ответ не делит объект)."""
    return STUB_AGENT.model_copy()


def stub_config() -> StateConfig:
    """Поток конфигурации заглушки: версия, каркас и контрольная сумма от него самого."""
    return StateConfig(
        version=STUB_CONFIG_VERSION, checksum=STUB_CONFIG_CHECKSUM, json=STUB_CONFIG_DOCUMENT
    )


# Отказы раздела несут ``Retry-After``: иначе частоту повторов определяет только агент, а
# сервер на неё повлиять не может. Подтверждение ноды — действие администратора (UC-01 шаг 7),
# поэтому пауза длинная; несовпадение поколения агент исправляет сам, снапшотом, — короткая.
RETRY_AFTER_APPROVAL = 300
RETRY_AFTER_RESYNC = 1


def node_not_approved() -> ApiError:
    """``pending``: нода зарегистрирована, но не подтверждена — раздел ей ничего не выдаёт
    (§4.6, матрица влияния: ни потоков, ни канала команд)."""
    return ApiError(
        "node_not_approved",
        "нода не подтверждена администратором",
        status=403,
        headers={"Retry-After": str(RETRY_AFTER_APPROVAL)},
    )


def cursor_ahead() -> ApiError:
    """Нода предъявила курсор больше того, что выдавал Control Plane. Так бывает после
    восстановления из резервной копии (§9) и при подделке: в обоих случаях дельты неприменимы, и
    единственный ответ — снапшот. Молчаливые 204 здесь недопустимы: нода, объявившая себя
    впереди, перестала бы получать отзывы доступа, а сервер считал бы её синхронизированной
    (§11.3 — нода недоверенная)."""
    return ApiError(
        "cursor_ahead",
        "курсор ноды опережает состояние Control Plane: требуется полный снапшот",
        status=409,
        headers={"Retry-After": str(RETRY_AFTER_RESYNC)},
    )


def generation_mismatch(expected: int) -> ApiError:
    """Нода предъявила чужое поколение identity: дельты к её состоянию неприменимы, нужен
    снапшот с текущим поколением (§5.2, код 409)."""
    return ApiError(
        "generation_mismatch",
        "поколение identity не совпадает: требуется полный снапшот",
        status=409,
        details={"generation": expected},
        headers={"Retry-After": str(RETRY_AFTER_RESYNC)},
    )


@dataclass(frozen=True, slots=True)
class StoredRow:
    """Строка ``node_user_state`` в форме хранения: credentials пары — зашифрованными
    контейнерами ``node_user_credentials`` (§7.2), ``None`` — записи нет."""

    user_id: uuid.UUID
    state: UserState
    quota_grant_bytes: int
    blocked_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address]
    updated_seq: int
    vless_uuid_enc: bytes | None
    trojan_password_enc: bytes | None
    credentials_version: int | None


# Какие строки читает выдача: полный состав, дельту от курсора ноды или никаких.
RowsRead = Literal["snapshot", "delta", "none"]


@dataclass(frozen=True, slots=True)
class NodeComposition:
    """Состав ноды в одном снимке базы: ``nodes.desired_users_seq``, ``nodes.resync_required``
    и выбранные строки."""

    users_seq: int
    resync_required: bool
    rows: list[StoredRow]


async def next_users_seq(conn: Any, node_id: uuid.UUID) -> int:
    """Номер изменения состава ноды — единственный способ выделить ``updated_seq``
    (data-model §4.2.3 «Выделение»): ``UPDATE nodes … RETURNING`` в транзакции вызывающего, той
    же, что пишет строку. Блокировка строки ноды держится до коммита, поэтому номера одной ноды
    коммитятся по возрастанию; номер последовательности выдавался бы до коммита, и агент,
    продвинув курсор, пропустил бы строку, закоммиченную позже меньшим номером."""
    seq = await conn.fetchval(_NEXT_USERS_SEQ, node_id)
    if seq is None:
        raise LookupError(f"нода {node_id} не найдена")
    return int(seq)


_NEXT_USERS_SEQ = (
    "update nodes set desired_users_seq = desired_users_seq + 1 where id = $1 "
    "returning desired_users_seq"
)
# Ноды, которых может коснуться публикация пользователя: пересечение групп доступа тарифа его
# текущего периода с группами нод (§11.3) и ноды, где у него есть строка не в ``removed`` (выход
# из групп снимает доступ там). Порядок — по ``node_id``: так две публикации с пересекающимися
# наборами нод берут блокировки строк нод в одном порядке. Список — кандидаты: принадлежность
# группе перепроверяется под блокировкой строки ноды (``_PAIR``).
_CANDIDATES = """
select node_id from (
    select nag.node_id
      from subscriptions s
      join subscription_periods p on p.id = s.current_period_id
      join plan_access_groups pag on pag.plan_id = p.plan_id
      join node_access_groups nag on nag.access_group_id = pag.access_group_id
     where s.user_id = $1 and s.state <> 'none'
    union
    select node_id from node_user_state where user_id = $1 and state <> 'removed'
) candidates
order by node_id
"""
# Публикации одного пользователя идут по очереди: сессионная advisory-блокировка подключения
# исполнителя (пространство ключей ``PUBLISH_LOCK_CLASS``, ключ — хеш ``user_id``) берётся до
# выбора кандидатов и снимается после последней ноды. Без неё запуск B (после смены тарифа) мог
# выбрать кандидатов, пока запуск A ещё пишет незакоммиченную строку на ноде прежнего тарифа: B её
# не видит и не снимает, A коммитит доступ, которого новый тариф не даёт.
PUBLISH_LOCK_CLASS = 2901
_LOCK_USER = "select pg_advisory_lock($1, hashtext($2))"
_UNLOCK_USER = "select pg_advisory_unlock($1, hashtext($2))"
# Блокировка строки ноды — ``FOR NO KEY UPDATE``: тот же режим берут ``UPDATE nodes …`` другой
# публикации и приём отчёта (``accounting/service.py``), поэтому чтение состояния пары ждёт их
# коммита. Режимы слабее (``KEY SHARE``, ``SHARE``) этого не дают: с ``KEY SHARE`` чтение не ждёт
# конкурирующую запись, с ``SHARE`` две публикации на одной ноде ловят взаимную блокировку.
_LOCK_NODE = "select id from nodes where id = $1 for no key update"
# Состояние пары, прочитанное под блокировкой строки ноды: подписка, общая группа тарифа и
# ноды, нынешняя строка и запись credentials, заблокированные адреса пользователя (R-27).
_PAIR = """
select s.state::text as subscription_state,
       s.state is not null and s.state <> 'none' and exists (
           select 1
             from subscription_periods p
             join plan_access_groups pag on pag.plan_id = p.plan_id
             join node_access_groups nag on nag.access_group_id = pag.access_group_id
            where p.id = s.current_period_id and nag.node_id = $2
       ) as shares_group,
       r.state::text as row_state,
       c.user_id is not null as has_credentials,
       c.vless_uuid_enc, c.trojan_password_enc, c.version as credentials_version,
       array(select b.ip from user_blocked_ips b where b.user_id = $1 order by b.ip)
           as blocked_ips
  from (select $1::uuid as user_id) u
  left join subscriptions s on s.user_id = u.user_id
  left join node_user_state r on r.user_id = u.user_id and r.node_id = $2
  left join node_user_credentials c on c.user_id = u.user_id and c.node_id = $2
"""
# Размер гранта (``quota_grant_bytes``) публикация не трогает: его пишет служба грантов (001.36),
# новая строка получает значение столбца по умолчанию.
_UPSERT_ROW = """
insert into node_user_state (node_id, user_id, state, blocked_ips, updated_seq)
values ($1, $2, $3, $4::inet[], $5)
on conflict (node_id, user_id) do update
   set state = excluded.state, blocked_ips = excluded.blocked_ips,
       updated_seq = excluded.updated_seq, updated_at = now()
"""
_INSERT_CREDENTIALS = """
insert into node_user_credentials
       (user_id, node_id, xray_email, vless_uuid_enc, trojan_password_enc)
values ($1, $2, $3, $4, $5)
on conflict (user_id, node_id) do nothing
"""
_DELETE_CREDENTIALS = "delete from node_user_credentials where user_id = $1 and node_id = $2"
# Замена непригодных контейнеров пары новыми ключами: версия растёт, как при ротации (WI-38).
_REPLACE_CREDENTIALS = """
update node_user_credentials
   set vless_uuid_enc = $3, trojan_password_enc = $4, version = version + 1, rotated_at = now()
 where user_id = $1 and node_id = $2
"""


def credentials_bound(column: str, user_id: uuid.UUID, node_id: uuid.UUID) -> Binding:
    """Привязка контейнера credentials к паре «пользователь × нода» (WI-38): контейнер другого
    пользователя или той же пары на другой ноде не расшифровывается (§11.3)."""
    return bound_to("node_user_credentials", column, user_id, node_id)


_READ_NODE = "select desired_users_seq, resync_required from nodes where id = $1"
_ROW_COLUMNS = """
select s.user_id, s.state::text as state, s.quota_grant_bytes, s.blocked_ips, s.updated_seq,
       c.vless_uuid_enc, c.trojan_password_enc, c.version as credentials_version
  from node_user_state s
  left join node_user_credentials c on c.node_id = s.node_id and c.user_id = s.user_id
"""
# Дельта — строки с номером больше курсора ноды, включая ``removed``; снапшот — все строки
# ноды, кроме ``removed`` (§4.2.3): отсутствующих в полном ответе нода удаляет сама.
_READ_DELTA = _ROW_COLUMNS + " where s.node_id = $1 and s.updated_seq > $2 order by s.updated_seq"
_READ_SNAPSHOT = (
    _ROW_COLUMNS + " where s.node_id = $1 and s.state <> 'removed' order by s.updated_seq"
)
_MARK_RESYNC = "update nodes set resync_required = true where id = $1 and not resync_required"


class _KeysWithheld(Exception):  # noqa: N818 — сигнал публикации, не ошибка
    """Строке пары нужны ключи, а шифр процесса не сверен: транзакция ноды откатывается до
    выделения номера, публикация идёт к следующей ноде (``publish_user_on``)."""


class CompositionService:
    """Потоки конфигурации и состава поверх пула asyncpg. Состав (001.29): исполнитель
    публикации пишет ``node_user_state`` и ``node_user_credentials`` и будит канал Redis
    ``node:{id}``, выдача читает строки ноды из базы. Поток конфигурации и раздел ``agent`` —
    фиксированные значения до 001.29c и владельцев полей раздела."""

    def __init__(
        self,
        pool: Any,
        commands: CommandService | None = None,
        cipher: FieldCipher | None = None,
    ) -> None:
        self._pool = pool
        self._commands = commands or CommandService(pool)
        self._cipher = cipher

    async def _field_cipher(self) -> FieldCipher:
        """Шифр credentials (§7.2): шифр процесса, сверенный с контрольным контейнером базы
        (``checked_cipher``, WI-38: с чужим ключом ключи не выдаются и не пишутся, а пары не
        выглядят подменёнными), или переданный при создании — только герметичным прогонам без
        базы (раскладка фикстур); боевые вызовы шифр не передают."""
        if self._cipher is None:
            self._cipher = await checked_cipher()
        return self._cipher

    @staticmethod
    def _open(
        cipher: FieldCipher,
        user_id: uuid.UUID,
        node_id: uuid.UUID,
        vless: bytes | None,
        trojan: bytes | None,
        version: int | None,
    ) -> Credentials | None:
        """Credentials пары или ``None``, если пара непригодна: записи нет, контейнер не
        расшифровывается привязкой пары (подменён или повреждён, WI-38) или его текст не
        разбирается."""
        if vless is None or trojan is None or version is None:
            return None
        # Привязки — до разбора: ошибка построения привязки — дефект кода, и она обязана
        # упасть громко, а не выдать каждую пару непригодной.
        vless_bound = credentials_bound("vless_uuid_enc", user_id, node_id)
        trojan_bound = credentials_bound("trojan_password_enc", user_id, node_id)
        try:
            return Credentials(
                vless_uuid=uuid.UUID(bytes=cipher.decrypt(vless, bound=vless_bound)),
                trojan_password=cipher.decrypt(trojan, bound=trojan_bound).decode(),
                version=version,
            )
        except InvalidTag, ValueError:  # ValueError: и UnicodeDecodeError, и ValidationError
            return None

    async def _wake(self, node_id: uuid.UUID, users_seq: int) -> bool:
        """``PUBLISH node:{id}`` номером строки после коммита — пробуждение удержанного запроса
        состояния (§5.2 «Long-poll»); ``False`` — Redis недоступен. Потеря сообщения стоит до
        30 с таймаута удержания, а не доставки (раскладка Н-13, interfaces.md §5.4), поэтому сбой
        Redis пишется в журнал и публикацию не валит: строки уже закоммичены, и повтор задачи
        выдал бы им новые номера без нужды."""
        try:
            redis = await get_redis()
            await redis.publish(f"node:{node_id}", str(users_seq))
        except RedisError as exc:
            log.warning("node:%s: пробуждение не отправлено (%s)", node_id, exc)
            return False
        return True

    async def publish_user(self, user_id: uuid.UUID) -> None:
        """Опубликовать доступ пользователя на его нодах на подключении из пула (прямые вызовы
        ``SubscriptionService`` 001.22 после коммита — до перевода на очередь в 001.29b)."""
        async with self._pool.acquire() as conn:
            await self.publish_user_on(conn, user_id)

    async def publish_user_on(self, conn: Any, user_id: uuid.UUID) -> None:
        """Исполнитель задачи ``composition.publish_user`` на подключении вызывающего, которое
        не в транзакции (иначе транзакции нод стали бы точками сохранения, блокировки нод жили бы
        до внешнего коммита, а снятая блокировка пользователя пустила бы следующий запуск к
        незакоммиченным строкам — ``RuntimeError`` до любой записи): по каждой ноде-кандидату
        (``_CANDIDATES``, порядок по ``node_id``) — своя транзакция; ``PUBLISH node:{id}`` —
        после коммита всех нод.

        Транзакция на ноду, а не одна на всех: две публикации с пересекающимися наборами нод не
        ждут друг друга по кругу, а приём отчёта, держащий строку ноды, ждёт не дольше записи
        одной ноды. Сбой посередине оставляет закоммиченными первые ноды и пробрасывается — задача
        повторяется целиком, и повтор записывает каждую ноду заново: нода с прежним курсором
        получит ту же строку ещё раз, применение идемпотентно.

        Публикации одного пользователя сериализованы (``_LOCK_USER``). Пробуждения уходят после
        коммита всех нод: недоступный Redis задерживает только пробуждения (до тайм-аутов
        клиента на каждое), а не запись следующих нод.

        Ключ процесса не тот (``CipherMismatch`` сверки, WI-38): строки, которым ключи не нужны
        (отзывы — ``removed`` и состояния без доступа), пишутся и будят ноды; пары, которым нужны
        ключи (``active``), пропускаются, и после пробуждений публикация отказывает тем же
        ``CipherMismatch`` — задача повторяется (решение заказчика 2026-11-05): приостановка
        доходит до нод и при чужом ключе."""
        if conn.is_in_transaction():
            raise RuntimeError("публикация состава идёт на подключении вне транзакции")
        mismatch: CipherMismatch | None = None
        cipher: FieldCipher | None = None
        try:
            cipher = await self._field_cipher()
        except CipherMismatch as exc:
            mismatch = exc
        key = str(user_id)
        written: list[tuple[uuid.UUID, int]] = []
        held: list[uuid.UUID] = []
        await conn.execute(_LOCK_USER, PUBLISH_LOCK_CLASS, key)
        try:
            candidates = [row["node_id"] for row in await conn.fetch(_CANDIDATES, user_id)]
            for node_id in candidates:
                try:
                    async with conn.transaction():
                        seq = await self._publish_pair(conn, user_id, node_id, cipher)
                except _KeysWithheld:
                    held.append(node_id)
                    continue
                if seq is not None:
                    written.append((node_id, seq))
        finally:
            if not conn.is_closed():
                await conn.execute(_UNLOCK_USER, PUBLISH_LOCK_CLASS, key)
        for position, (node_id, seq) in enumerate(written):
            if not await self._wake(node_id, seq):
                skipped = len(written) - position - 1
                if skipped:
                    log.warning(
                        "пользователь %s: Redis недоступен, пробуждения ещё %d нод пропущены",
                        user_id,
                        skipped,
                    )
                break
        if held and mismatch is not None:
            log.error(
                "пользователь %s: ключ процесса не сверен — строки active на %d нодах не записаны",
                user_id,
                len(held),
            )
            raise mismatch

    async def _lock_node(self, conn: Any, node_id: uuid.UUID) -> bool:
        """Блокировка строки ноды ``FOR NO KEY UPDATE`` в транзакции вызывающего (``_LOCK_NODE``);
        ``False`` — ноды нет."""
        return await conn.fetchval(_LOCK_NODE, node_id) is not None

    async def _publish_pair(
        self, conn: Any, user_id: uuid.UUID, node_id: uuid.UUID, cipher: FieldCipher | None
    ) -> int | None:
        """Строка пары «пользователь × нода» в транзакции вызывающего; номер записанной строки
        или ``None``, если писать нечего. ``cipher`` — сверенный шифр процесса или ``None``, если
        сверка не прошла: строка ``active`` тогда не пишется (``_KeysWithheld`` — до выделения
        номера), а у пары в состоянии без доступа создание и замена credentials откладываются до
        публикации со сверенным шифром — строке без доступа ключи не нужны.

        Состояние пользователя читается **после** блокировки строки ноды. Две публикации одного
        пользователя уже идут по очереди (``_LOCK_USER``); чтение под блокировкой ноды — против
        писателей строк пользователя на этой ноде, которые блокировку пользователя не берут
        (состав ноды целиком — 001.29b, ротация credentials — 001.29d): запуск, прочитавший
        состояние до блокировки, мог бы ждать её, пока такой писатель пишет новое состояние, и
        записать старое с большим номером — у ноды остался бы снятый доступ.

        Тариф делит группу с нодой — строка в состоянии подписки, credentials пары создаются
        при отсутствии; не делит, а строка есть и не ``removed`` — строка ``removed``, запись
        credentials удаляется (data-model §4.2.3 «Состав строк»). Номер выделяется, только когда
        строка пишется: пустой номер оставил бы дыру в курсоре ноды."""
        if not await self._lock_node(conn, node_id):
            return None  # нода исчезла между выбором кандидатов и блокировкой
        pair = await conn.fetchrow(_PAIR, user_id, node_id)
        shares = bool(pair["shares_group"])
        if not shares and pair["row_state"] in (None, "removed"):
            return None
        state = pair["subscription_state"] if shares else "removed"
        if cipher is None and state == "active":
            raise _KeysWithheld
        seq = await next_users_seq(conn, node_id)
        await conn.execute(
            _UPSERT_ROW,
            node_id,
            user_id,
            state,
            list(pair["blocked_ips"]) if shares else [],
            seq,
        )
        if shares and cipher is not None:

            def fresh() -> tuple[bytes, bytes]:
                return (
                    cipher.encrypt(
                        uuid.uuid4().bytes,
                        bound=credentials_bound("vless_uuid_enc", user_id, node_id),
                    ),
                    cipher.encrypt(
                        secrets.token_urlsafe(24).encode(),
                        bound=credentials_bound("trojan_password_enc", user_id, node_id),
                    ),
                )

            if not pair["has_credentials"]:
                await conn.execute(
                    _INSERT_CREDENTIALS, user_id, node_id, xray_email(user_id), *fresh()
                )
            elif (
                self._open(
                    cipher,
                    user_id,
                    node_id,
                    pair["vless_uuid_enc"],
                    pair["trojan_password_enc"],
                    pair["credentials_version"],
                )
                is None
            ):
                # Контейнеры пары непригодны (решение заказчика 2026-11-05, WI-38): публикация
                # выдаёт паре новые ключи — версия растёт, нода получит строку с новыми ключами.
                await conn.execute(_REPLACE_CREDENTIALS, user_id, node_id, *fresh())
                log.warning(
                    "пара %s×%s: credentials не расшифровывались — выданы новые", node_id, user_id
                )
        elif not shares and pair["has_credentials"]:
            await conn.execute(_DELETE_CREDENTIALS, user_id, node_id)
        return seq

    async def _read(self, node_id: uuid.UUID, users_seq: int, rows: RowsRead) -> NodeComposition:
        """Курсор ноды, её признак ``resync_required`` и строки — в одном снимке базы
        (``repeatable read``): строка, закоммиченная между чтением курсора и строк, не попадёт в
        ответ без курсора, который её покрывает. ``rows``: ``snapshot`` — все строки, кроме
        ``removed``; ``delta`` — с номером больше ``users_seq``; ``none`` — без строк."""
        async with (
            self._pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            node = await conn.fetchrow(_READ_NODE, node_id)
            if node is None:
                raise LookupError(f"нода {node_id} не найдена")
            records: list[Any] = []
            if rows == "snapshot":
                records = await conn.fetch(_READ_SNAPSHOT, node_id)
            elif rows == "delta":
                records = await conn.fetch(_READ_DELTA, node_id, users_seq)
        return NodeComposition(
            users_seq=int(node["desired_users_seq"]),
            resync_required=bool(node["resync_required"]),
            rows=[
                StoredRow(
                    user_id=r["user_id"],
                    state=r["state"],
                    quota_grant_bytes=int(r["quota_grant_bytes"]),
                    blocked_ips=list(r["blocked_ips"]),
                    updated_seq=int(r["updated_seq"]),
                    vless_uuid_enc=r["vless_uuid_enc"],
                    trojan_password_enc=r["trojan_password_enc"],
                    credentials_version=r["credentials_version"],
                )
                for r in records
            ],
        )

    async def _mark_resync(self, node_id: uuid.UUID) -> None:
        """Взвести ``nodes.resync_required``: ответ отфильтрован статусом, и при выходе из
        статуса нода обязана взять снапшот (§5.2). Снятие — подтверждением полного ответа
        (001.75)."""
        async with self._pool.acquire() as conn:
            await conn.execute(_MARK_RESYNC, node_id)

    def _row(self, node_id: uuid.UUID, stored: StoredRow, cipher: FieldCipher | None) -> UserRow:
        """Строка ответа. Credentials расшифровываются только у строки, выдающей доступ, — и
        только из строк ноды запроса: выборка идёт по ``node_id`` ноды из identity (§5.2), а
        контейнер привязан к паре (``credentials_bound``).

        Пара в ``active`` с непригодными credentials (записи нет, контейнер подменён или
        повреждён, текст не разбирается) выдаётся строкой ``removed`` без ключей: пользователь
        теряет доступ на ноде, прежние и чужие ключи там не остаются; остальной ответ и отзывы
        выдаются (решение заказчика 2026-11-05, WI-38). Новые ключи паре выдаёт следующая
        публикация."""
        credentials = None
        if stored.state == "active":
            if cipher is None:
                raise RuntimeError("шифр берётся до разбора строк active")
            credentials = self._open(
                cipher,
                stored.user_id,
                node_id,
                stored.vless_uuid_enc,
                stored.trojan_password_enc,
                stored.credentials_version,
            )
            if credentials is None:
                log.error(
                    "пара %s×%s: credentials непригодны — выдана строка removed",
                    node_id,
                    stored.user_id,
                )
                return UserRow(
                    user_id=stored.user_id,
                    state="removed",
                    xray_email=xray_email(stored.user_id),
                    quota_grant_bytes=0,
                    blocked_ips=[],
                    updated_seq=stored.updated_seq,
                )
        return UserRow(
            user_id=stored.user_id,
            state=stored.state,
            xray_email=xray_email(stored.user_id),
            quota_grant_bytes=stored.quota_grant_bytes,
            blocked_ips=stored.blocked_ips,
            updated_seq=stored.updated_seq,
            credentials=credentials,
        )

    async def state_for(
        self,
        node: Node,
        config_version: int,
        users_seq: int,
        generation: int,
        full: bool,
        agent_section_version: int,
    ) -> StateResponse | None:
        """Состояние для ноды (§5.2). ``None`` — изменений нет: маршрут отвечает 204.

        Вентиль по статусу §4.6 применяется до всего остального и к обеим формам запроса —
        снапшот его не обходит: ``pending`` — отказ 403; поколение, отличное от действующего, —
        409 (нода обязана взять снапшот). Поколение заглушки фиксировано; смысл ``generation``
        и его сверка — 001.75b, окно перекрытия при ротации identity (Н-28) — 001.31.

        Состав (001.29) — строки ``node_user_state`` ноды запроса: курсор ноды
        (``desired_users_seq``), её ``resync_required`` и строки читаются в одном снимке базы
        (``_read``); дельта — строки с номером больше курсора ноды, снапшот — все, кроме
        ``removed``. Урезанный вентилем ответ взводит ``resync_required`` в колонке ноды.

        «Нода отстала» считается только по потокам, которые вентиль открывает. Иначе нода в
        ``provisioning`` (состав закрыт, курсор состава не двигается) отставала бы по нему
        вечно и получала немедленный ответ на каждый запрос — вместо удержания до таймаута,
        которого требует §5.2 именно для этого статуса.

        Инвариант для 001.75c, который здесь соблюдён и обязан сохраниться: удержание long-poll
        не должно держать ни подключения из пула asyncpg (служба берёт пул, а не подключение),
        ни разделяемый клиент Redis — у него ``socket_timeout`` 2 с (``app.redis``), и наивный
        ``pubsub.listen()`` на 30 с даст 503 вместо 204.

        Раздел ``agent`` (001.26) ведёт свой курсор ``agent_section_version`` по тем же правилам,
        что ``config_version``: отставание — повод ответить, опережение — 409 ``cursor_ahead``,
        снапшот отдаёт раздел целиком. Вентиль у раздела — вентиль потока конфигурации: это
        параметры той же природы (структура ноды, а не состав), и нода в ``disabled`` без
        стратегии разрыва не смогла бы применить отзывы, которые ей ещё доставляются.
        """
        gate = STREAM_GATE[node.status]
        if gate.refused:
            raise node_not_approved()
        if generation != STUB_GENERATION:
            raise generation_mismatch(STUB_GENERATION)
        composition_open = gate.composition != "none"
        # Полным состав считается, только когда вентиль отдаёт его целиком: снапшот в
        # фильтрующем статусе полным не является, и нода, приняв его за полный, вычистила бы у
        # себя всех, кого вентиль скрыл (§4.2.3). Такой «снапшот» читается дельтой: отсутствие
        # строки означает «удали» только в полном ответе, и урезанный обязан везти ``removed``
        # строкой — иначе отключённая нода, взяв снапшот по указанию ``resync_required``,
        # получила бы меньше отзывов, чем дала бы обычная дельта.
        complete = full and gate.composition == "all"
        read: RowsRead = "snapshot" if complete else "delta" if composition_open else "none"
        # Снапшот присланный курсор игнорирует (README контракта): нода просит его как раз тогда,
        # когда её курсору нельзя верить (курсор впереди после восстановления, смена поколения).
        # Урезанный «снапшот» читает дельту от нуля — все строки ноды, включая ``removed``.
        stored = await self._read(node.id, 0 if full else users_seq, read)
        current = stored.users_seq
        # Курсор впереди серверного бывает после восстановления Control Plane из копии (§9) и
        # при подделке; дельта к такому состоянию неприменима. Снапшот — исключение и есть выход
        # из него: он присланный курсор игнорирует и сам ставит ``seq = current``. Отказывать и
        # ему значило бы оставить ноду без выхода: отказ велит взять снапшот, а снапшот отвечает
        # тем же отказом.
        if not full and (
            config_version > STUB_CONFIG_VERSION
            or agent_section_version > STUB_AGENT_VERSION
            or users_seq > current
        ):
            raise cursor_ahead()
        config = (
            stub_config()
            if gate.config and (full or config_version < STUB_CONFIG_VERSION)
            else None
        )
        agent = (
            stub_agent()
            if gate.config and (full or agent_section_version < STUB_AGENT_VERSION)
            else None
        )
        behind = composition_open and (full or users_seq < current)
        # Взведённый признак — самостоятельный повод ответить, но только там, где его можно
        # снять, — в статусе, вентиль которого отдаёт состав целиком: §5.2 требует показать его
        # при выходе из фильтрующего статуса, а нода к этому моменту уже на текущих курсорах, и
        # молчаливый 204 съел бы поручение ровно там, где оно нужно. Где состав урезан
        # (``disabled``, ``suspended``) или закрыт (``provisioning``), снять признак нечем
        # (снимает подтверждение полного ответа, 001.75, а полного ответа вентиль не даёт), и
        # повод «признак взведён» не дал бы ноде ни одного 204 — она не дожидалась бы удержания
        # и крутила бы запросы вхолостую.
        owes_resync = stored.resync_required and gate.composition == "all"
        if config is None and agent is None and not behind and not owes_resync:
            return None
        rows: list[UserRow] = []
        seq = users_seq
        if composition_open:
            selected = stored.rows
            # Курсор продвигается по выборке — до фильтра по статусу (§5.2). Считать его по
            # выданным строкам значило бы застревать всякий раз, когда самое свежее изменение
            # вентиль скрыл: нода просила бы ту же дельту снова и снова. Пропущенное она добирает
            # снапшотом после выхода из статуса — для того и ``resync_required``. Курсор
            # снапшота — текущий номер ноды: ``removed`` в снапшот не входит, и номер удалённой
            # пары пришёл бы ноде ещё раз следующей дельтой.
            seq = current if full else max((row.updated_seq for row in selected), default=current)
            kept = (
                [row for row in selected if row.state in REVOKING_STATES]
                if gate.composition == "revocations"
                else selected
            )
            # Шифр — только под строки active: ответ из одних отзывов (дельта, вентиль ``disabled``
            # и ``suspended``) выдаётся и при чужом ключе процесса (WI-38).
            cipher = (
                await self._field_cipher() if any(row.state == "active" for row in kept) else None
            )
            rows = [self._row(node.id, row, cipher) for row in kept]
        resync_required = stored.resync_required
        if gate.filters_composition:
            # Ответ урезан вентилем: признак взводится в колонке ноды (data-model §4.2.3) и
            # переживает статус — при выходе из него нода на текущих курсорах получит его
            # из колонки, а не из статуса, который уже ничего не фильтрует.
            await self._mark_resync(node.id)
            resync_required = True
        return StateResponse(
            config=config,
            agent=agent,
            users=Users(seq=seq, full=complete, rows=rows),
            generation=STUB_GENERATION,
            # Полный состав — это и есть выполненная пересинхронизация, поэтому в таком ответе
            # признак не показывается: иначе нода, применив снапшот, тут же пошла бы за ним
            # снова. Колонку снимает подтверждение курсора полного ответа (001.75); условие
            # остановки для агента наблюдаемо — ``users.full``.
            resync_required=resync_required and not complete,
            # Канал команд: заглушка не помнит доставку, поэтому её канал никогда не пуст —
            # включить его в решение «изменений нет» значило бы сделать 204 недостижимым, а
            # удержание long-poll — невозможным. Отсюда соглашение заглушки: команда едет вместе
            # с ответом, который и так отправляется. В 001.76 ожидающая команда обязана сама
            # быть причиной ответить: канал проверяется до решения о 204.
            commands=await self._commands.pending(node) if gate.commands else [],
        )

    async def ack(self, node: Node, config_version: int, users_seq: int) -> None:
        """Принять подтверждение применённых курсоров (``nodes.applied_*``, ``applied_at``).

        Принимает ноду, а не её идентификатор (отклонение от сигнатуры задачи): вентиль §4.6
        решает и здесь — неподтверждённая нода не вправе объявить себя применившей то, чего ей
        не выдавали. Запись ``applied_*`` и отказ подтверждению выше выданного курсора — 001.75.
        Там же снятие ``resync_required`` — но **только** по подтверждению курсора ответа,
        пришедшего с ``users.full: true``: снять признак по подтверждению урезанного ответа
        значило бы закрыть ноде пересинхронизацию, которой не было."""
        if STREAM_GATE[node.status].refused:
            raise node_not_approved()

    async def publish_config(self, node_id: uuid.UUID) -> None:
        """Новая версия конфигурации ноды: запись ``node_config_versions`` и ``PUBLISH
        node:{id}`` — 001.29c (генератор конфигурации — 001.27)."""

    async def remove_user(self, user_id: uuid.UUID) -> None:
        """Снять доступ пользователя: строки в ``removed`` и отзыв credentials — 001.29b
        (бюджет Н-13 на доставку отзыва — §5.4)."""
